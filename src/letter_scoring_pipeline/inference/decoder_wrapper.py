"""Decoder wrapper that can switch between Python and Rust implementations."""

import os
from typing import Any

import numpy as np
from loguru import logger

from .constants import VALID_PHONETIC_ELEMENTS
from .decoder import PhonemeDecoder
from .models import PhoneticTranscript

try:
    from my_asr_aligner import RustPhonemeDecoder

    RUST_DECODER_AVAILABLE = True
    logger.info("Rust decoder loaded successfully")
except (ImportError, AttributeError) as e:
    RUST_DECODER_AVAILABLE = False
    logger.warning(f"Rust decoder not available: {e}, falling back to Python")


class DecoderWrapper:
    """Wrapper that switches between Python and Rust decoder implementations."""

    def __init__(self, *, phonetic_elements: list[str] | None = None, use_rust: bool | None = None):
        """Initialize the decoder wrapper.

        Args:
            phonetic_elements: Optional override of valid phonetic elements.
            use_rust: Force use of Rust decoder (True) or Python decoder (False).
                     If None, determined by USE_RUST_DECODER env var.
        """
        self.phonetic_elements = phonetic_elements or VALID_PHONETIC_ELEMENTS

        # Type annotations for decoder instances
        self._python_decoder: PhonemeDecoder | None
        self._decoder: Any  # RustPhonemeDecoder when available

        if use_rust is None:
            use_rust = os.getenv("USE_RUST_DECODER", "true").lower() in {"1", "true", "yes", "on"}

        self.use_rust = use_rust and RUST_DECODER_AVAILABLE

        if self.use_rust:
            logger.info("Using Rust phoneme decoder for high performance")
            self._decoder = RustPhonemeDecoder(self.phonetic_elements)
            self._python_decoder = None  # Lazy load if needed
        else:
            logger.info("Using Python phoneme decoder")
            self._python_decoder = PhonemeDecoder(phonetic_elements=self.phonetic_elements)
            self._decoder = None

    def decode(self, *, pred_tokens: list[str], max_probs: np.ndarray | None) -> PhoneticTranscript:
        """Decode predicted tokens to a phonetic transcript.

        Args:
            pred_tokens: Sequence of tokens produced by the acoustic model.
            max_probs: Optional per-token maximum probabilities.

        Returns:
            PhoneticTranscript: Parsed phonetic elements and confidences.
        """
        if self.use_rust:
            probs_list = None
            if max_probs is not None:
                probs_list = max_probs.tolist() if hasattr(max_probs, "tolist") else list(max_probs)

            try:
                if self._decoder is None:
                    raise RuntimeError("Rust decoder not initialized")
                elements, confidences = self._decoder.decode(pred_tokens, probs_list)
                return PhoneticTranscript(elements=elements, confidences=confidences)
            except Exception as e:
                logger.warning(f"Rust decoder failed: {e}, falling back to Python")
                if self._python_decoder is None:
                    self._python_decoder = PhonemeDecoder(phonetic_elements=self.phonetic_elements)
                if self._python_decoder is None:
                    raise RuntimeError("Failed to initialize Python decoder fallback")
                return self._python_decoder.decode(pred_tokens=pred_tokens, max_probs=max_probs)
        else:
            if self._python_decoder is None:
                raise RuntimeError("Python decoder not initialized")
            return self._python_decoder.decode(pred_tokens=pred_tokens, max_probs=max_probs)

    def decode_batch(
        self, *, batch_tokens: list[list[str]], batch_probs: list[np.ndarray] | None
    ) -> list[PhoneticTranscript]:
        """Decode a batch of token sequences.

        Args:
            batch_tokens: List of token sequences.
            batch_probs: Optional list of probability arrays.

        Returns:
            List of PhoneticTranscript objects.
        """
        if self.use_rust:
            probs_lists = None
            if batch_probs is not None:
                probs_lists = [
                    probs.tolist() if hasattr(probs, "tolist") else list(probs)
                    for probs in batch_probs
                ]

            try:
                if self._decoder is None:
                    raise RuntimeError("Rust decoder not initialized")
                results = self._decoder.decode_batch(batch_tokens, probs_lists)
                return [
                    PhoneticTranscript(elements=elements, confidences=confidences)
                    for elements, confidences in results
                ]
            except Exception as e:
                logger.warning(f"Rust batch decoder failed: {e}, falling back to Python")
                if self._python_decoder is None:
                    self._python_decoder = PhonemeDecoder(phonetic_elements=self.phonetic_elements)
                if self._python_decoder is None:
                    raise RuntimeError("Failed to initialize Python decoder for batch processing")
                if batch_probs is not None:
                    return [
                        self._python_decoder.decode(pred_tokens=tokens, max_probs=probs)
                        for tokens, probs in zip(batch_tokens, batch_probs)
                    ]
                else:
                    return [
                        self._python_decoder.decode(pred_tokens=tokens, max_probs=None)
                        for tokens in batch_tokens
                    ]
        else:
            if self._python_decoder is None:
                raise RuntimeError("Python decoder not initialized")
            if batch_probs is not None:
                return [
                    self._python_decoder.decode(pred_tokens=tokens, max_probs=probs)
                    for tokens, probs in zip(batch_tokens, batch_probs)
                ]
            else:
                return [
                    self._python_decoder.decode(pred_tokens=tokens, max_probs=None)
                    for tokens in batch_tokens
                ]
