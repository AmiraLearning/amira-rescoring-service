import os

import numpy as np
from loguru import logger

from .constants import VALID_PHONETIC_ELEMENTS, TokenType
from .models import (
    GroupedPhoneticUnits,
    PhoneticTranscript,
    Segment,
)
from .phonetics import LongestMatchResult, PhoneticTrie


class PhonemeDecoder:
    """Decode model token outputs into phonetic/phonemic transcripts.

    This class encapsulates the token grouping and longest-match parsing
    against a phonetic trie and exposes a single entrypoint to decode a
    sequence of predicted tokens (and optional confidences) into a
    `PhoneticTranscript`.

    Args:
        phonetic_elements: Optional override of valid phonetic elements used
            to build the trie. Defaults to `VALID_PHONETIC_ELEMENTS`.
    """

    def __init__(self, *, phonetic_elements: list[str] | None = None) -> None:
        self._phonetic_trie = PhoneticTrie(
            phonetic_elements=phonetic_elements or VALID_PHONETIC_ELEMENTS
        )

    def decode(self, *, pred_tokens: list[str], max_probs: np.ndarray | None) -> PhoneticTranscript:
        """Decode predicted tokens to a phonetic transcript.

        Args:
            pred_tokens: Sequence of tokens produced by the acoustic model.
            max_probs: Optional per-token maximum probabilities aligned with
                `pred_tokens` for confidence aggregation.

        Returns:
            PhoneticTranscript: Parsed phonetic elements and aggregated confidences.
        """
        final_transcript: PhoneticTranscript = PhoneticTranscript(elements=[], confidences=[])
        current_segment: Segment = Segment(tokens=[], confidences=[])

        for index, token in enumerate(pred_tokens):
            if token == TokenType.PAD:
                continue
            elif token == TokenType.SEPARATOR:
                if current_segment.tokens:
                    segment_transcript: PhoneticTranscript = self._process_segment_to_phonetics(
                        segment=current_segment
                    )
                    final_transcript.elements.extend(segment_transcript.elements)
                    final_transcript.confidences.extend(segment_transcript.confidences)
                    current_segment = Segment(tokens=[], confidences=[])
            else:
                current_segment.tokens.append(token)
                if max_probs is not None:
                    current_segment.confidences.append(max_probs[index])

        if current_segment.tokens:
            segment_transcript = self._process_segment_to_phonetics(segment=current_segment)
            final_transcript.elements.extend(segment_transcript.elements)
            final_transcript.confidences.extend(segment_transcript.confidences)

        return final_transcript

    def _process_segment_to_phonetics(self, *, segment: Segment) -> PhoneticTranscript:
        """Process a segment of tokens to a phonetic transcript.

        Args:
            segment: The segment of tokens to process.

        Returns:
            PhoneticTranscript: The phonetic transcript.
        """
        grouped: GroupedPhoneticUnits = self._group_consecutive_tokens(segment=segment)
        parsed: GroupedPhoneticUnits = self._parse_phonetic_elements(grouped_segment_units=grouped)
        return PhoneticTranscript(elements=parsed.tokens, confidences=parsed.confidences)

    def _group_consecutive_tokens(self, *, segment: Segment) -> GroupedPhoneticUnits:
        """Group consecutive tokens in a segment.

        Args:
            segment: The segment of tokens to process.

        Returns:
            GroupedPhoneticUnits: The grouped phonetic units.
        """
        if not segment.tokens:
            return GroupedPhoneticUnits(tokens=[], confidences=[])

        grouped_tokens: list[str] = []
        averaged_confidences: list[float] = []

        has_confidences = segment.confidences is not None and len(segment.confidences) > 0

        current_token: str = segment.tokens[0]
        current_confidences: list[float] = []

        if has_confidences:
            current_confidences.append(float(segment.confidences[0].item()))

        for index in range(1, len(segment.tokens)):
            next_token: str = segment.tokens[index]
            if current_token == next_token:
                if has_confidences and len(segment.confidences) > index:
                    current_confidences.append(float(segment.confidences[index].item()))
            else:
                grouped_tokens.append(current_token)
                if has_confidences and current_confidences:
                    averaged_confidences.append(sum(current_confidences) / len(current_confidences))
                current_token = next_token
                current_confidences = []
                if has_confidences and len(segment.confidences) > index:
                    current_confidences.append(float(segment.confidences[index].item()))

        grouped_tokens.append(current_token)
        if has_confidences and current_confidences:
            averaged_confidences.append(sum(current_confidences) / len(current_confidences))

        return GroupedPhoneticUnits(tokens=grouped_tokens, confidences=averaged_confidences)

    def _parse_phonetic_elements(
        self, *, grouped_segment_units: GroupedPhoneticUnits
    ) -> GroupedPhoneticUnits:
        """Parse phonetic elements from a grouped segment.

        Args:
            grouped_segment_units: The grouped segment units to parse.

        Returns:
            GroupedPhoneticUnits: The parsed phonetic units.
        """
        final_elements: list[str] = []
        final_confidences: list[float] = []
        index: int = 0
        track_confidence: bool = bool(grouped_segment_units.confidences)

        robust_env = os.getenv("DECODER_ROBUST_MODE", "false").lower() in {"1", "true", "yes", "on"}
        while index < len(grouped_segment_units.tokens):
            longest_match: LongestMatchResult = self._phonetic_trie.find_longest_match(
                tokens=grouped_segment_units.tokens, start_index=index
            )
            if longest_match.matched_element and longest_match.tokens_consumed > 0:
                final_elements.append(longest_match.matched_element)
                if track_confidence:
                    segment_conf: list[float] = grouped_segment_units.confidences[
                        index : index + longest_match.tokens_consumed
                    ]
                    final_confidences.append(sum(segment_conf) / len(segment_conf))
                index += longest_match.tokens_consumed
            else:
                message: str = (
                    "Unable to match phonetic element at position "
                    f"{index} in segment: "
                    f"{''.join(grouped_segment_units.tokens[index:])}"
                )
                if robust_env:
                    logger.warning(message)
                    index += 1
                    continue
                logger.error(message)
                raise ValueError(message)

        return GroupedPhoneticUnits(tokens=final_elements, confidences=final_confidences)
