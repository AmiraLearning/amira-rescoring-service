"""Integration tests for the ORF rescoring pipeline processor using real data."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

import src.orf_rescoring_pipeline.core.processor as processor_module
import src.orf_rescoring_pipeline.rules.rescoring as rescoring_module
from src.orf_rescoring_pipeline.core.processor import process_single_activity
from src.orf_rescoring_pipeline.models import Activity, TranscriptItem, WordItem
from src.orf_rescoring_pipeline.rules.flagging import FlaggingAnalyzer
from src.orf_rescoring_pipeline.utils.debug import FlaggingStrategy


# Generic helper function for creating return-capturing spies
def create_return_capturing_spy(
    mocker: MockerFixture,
    target_module: Any,
    function_name: str,
) -> tuple[MagicMock, list[Any]]:
    """Create a spy that captures return values."""
    return_values: list[Any] = []
    original_func = getattr(target_module, function_name)

    def capture_returns(*args: Any, **kwargs: Any) -> Any:
        result = original_func(*args, **kwargs)
        return_values.append(result)
        return result

    spy = mocker.patch.object(target_module, function_name, side_effect=capture_returns)
    return spy, return_values


@pytest.mark.integration
@pytest.mark.requires_test_data
class TestProcessorIntegration:
    """Integration tests for process_single_activity with real data mocking."""

    async def test_complex_activity_integration(
        self: TestProcessorIntegration,
        mocker: MockerFixture,
        real_kaldi_transcripts: dict[str, list[str]],
        real_w2v_transcripts: dict[str, list[str]],
        real_model_features: dict[str, list[dict[str, Any]]],
        real_activities: dict[str, Activity],
        real_manifest_pages: dict[str, list[Any]],
        real_deepgram_transcripts: dict[str, dict[str, Any]],
        mock_audio_data: bytes,
        real_timing_files: dict[str, dict[str, Any]],
    ) -> None:
        """Test processing of Jane Goodall passage."""
        activity = real_activities["jane_goodall_full_rescore"]

        # Load real data for this activity
        deepgram_data = real_deepgram_transcripts[activity.activity_id]

        # Mock all external dependencies with MagicMock
        mock_appsync = MagicMock()
        mock_env = MagicMock()
        mock_env.audio_bucket = "test-bucket"

        # Mock Deepgram client with real transcript
        mock_deepgram = MagicMock()
        transcript_text = deepgram_data["transcript"]
        words = [
            WordItem(
                word=w["word"],
                start=w["start"],
                end=w["end"],
                confidence=w["confidence"],
            )
            for w in deepgram_data["words"]
        ]
        mock_deepgram.transcribe.return_value = TranscriptItem(
            transcript=transcript_text, words=words
        )

        mock_phrase_builder = MagicMock()
        mocker.patch(
            "src.orf_rescoring_pipeline.core.processor.PhraseBuilder",
            return_value=mock_phrase_builder,
        )
        mock_phrase_manifest = MagicMock()
        mock_phrase_manifest.generate.return_value = real_manifest_pages[activity.activity_id]
        mocker.patch(
            "src.orf_rescoring_pipeline.core.processor.PhraseManifest",
            return_value=mock_phrase_manifest,
        )

        # Mock the AppSync get_custom_activity_features method that get_model_features calls
        # This method returns a query function, so we need to return a callable that returns our data
        mock_query_function = MagicMock(return_value=real_model_features[activity.activity_id])
        mock_appsync.get_custom_activity_features.return_value = mock_query_function

        mock_kaldi = MagicMock()
        mock_w2v = MagicMock()

        # Create transcript tuples by pairing kaldi and w2v transcripts by phrase index
        kaldi_transcripts = real_kaldi_transcripts[activity.activity_id]
        w2v_transcripts = real_w2v_transcripts[activity.activity_id]
        transcript_tuples = [
            (kaldi_transcripts[i], w2v_transcripts[i]) for i in range(len(kaldi_transcripts))
        ]

        # Mock transcribe_phrase to return the appropriate tuple for each call
        mocker.patch(
            "src.orf_rescoring_pipeline.rules.rescoring.transcribe_phrase",
            side_effect=transcript_tuples,
        )

        # Mock audio loading from S3 - function doesn't return anything, just sets activity.audio_file_data
        def mock_load_audio(*, activity: Any, audio_bucket: str) -> None:
            activity.audio_file_data = mock_audio_data

        mocker.patch(
            "src.orf_rescoring_pipeline.core.processor.load_activity_audio_data_from_s3",
            side_effect=mock_load_audio,
        )

        # SPY on the key functions we want to verify
        spy_load_page_data = mocker.spy(activity, "load_page_data")

        # phrase alignment for deepgram
        spy_process_activity_timing = mocker.spy(processor_module, "process_activity_timing")
        print(spy_process_activity_timing.call_count)

        # Spy on word alignment functions at their import locations
        # For deepgram matches
        (
            spy_deepgram_word_alignment,
            deepgram_return_values,
        ) = create_return_capturing_spy(
            mocker, processor_module, "get_word_level_transcript_alignment"
        )

        spy_transcribe_phrase = mocker.spy(rescoring_module, "transcribe_phrase")

        # For kaldi matches on re-transcription
        (
            spy_rescoring_kaldi_word_alignment,
            kaldi_return_values,
        ) = create_return_capturing_spy(
            mocker, rescoring_module, "get_word_level_transcript_alignment"
        )

        # For w2v matches on re-transcription
        spy_rescoring_w2v_alignment, w2v_return_values = create_return_capturing_spy(
            mocker, rescoring_module, "get_word_level_transcript_alignment_w2v"
        )

        (
            spy_flagging_analyzer,
            flagging_analyzer_return_values,
        ) = create_return_capturing_spy(mocker, FlaggingAnalyzer, "decide_flagging_strategy")

        # Execute the processor
        result = await process_single_activity(
            activity=activity,
            appsync=mock_appsync,
            env=mock_env,
            deepgram=mock_deepgram,
            kaldi=mock_kaldi,
            w2v=mock_w2v,
            debug=False,
            save_files=False,
        )

        # ASSERTIONS

        # 1. Basic success
        assert result[0] is True, f"Processing should succeed for {activity.activity_id}"
        assert result[1] == activity.activity_id

        # 2. External API calls
        mock_appsync.get_custom_activity_features.assert_called_once()
        spy_load_page_data.assert_called_once()
        mock_appsync.set_activity_fields.assert_called_once()
        mock_deepgram.transcribe.assert_called_once()

        # 3. Activity-specific assertions
        assert len(activity.phrases) == 13, "This activity has 13 phrases"
        assert len(activity.pages) == 12, "This activity has 12 pages"

        # 4. activity page consistency checks
        phrase_indices_seen: set[int] = set()
        for page in activity.pages:
            for phrase_index in page.phrase_indices:
                assert phrase_index not in phrase_indices_seen, (
                    f"Phrase index {phrase_index} already seen"
                )
                assert all(phrase_index > idx for idx in phrase_indices_seen), (
                    f"Phrase index {phrase_index} is not greater than all previous phrase indices {phrase_indices_seen}"
                )
                phrase_indices_seen.add(phrase_index)

        # 5. Compare phrase alignment at page level to timing file
        ground_truth_pages = real_timing_files[activity.activity_id]["pages"]
        ground_truth_pages = sorted(ground_truth_pages, key=lambda x: x["page_index"])
        for i, page in enumerate(activity.pages):
            assert page.start_time == ground_truth_pages[i]["page_start"], (
                f"Page {i} start time do not match ground truth"
            )
            assert page.end_time == ground_truth_pages[i]["page_end"], (
                f"Page {i} end time do not match ground truth"
            )
            if page.aligned_phrases:
                for phrase in page.aligned_phrases:
                    assert page.phrase_indices == [
                        p["phrase_idx"] for p in ground_truth_pages[i]["phrases"]
                    ], f"Page {i} phrase indices do not match ground truth"
                    # The timing file contains absolute phrase indices, but the activity.pages contains relative indices in each page
                    assert (
                        phrase["start"]
                        == ground_truth_pages[i]["phrases"][phrase["phrase_idx"]]["start"]
                    ), f"Phrase {phrase['phrase_idx']} start time do not match ground truth"
                    assert (
                        phrase["end"]
                        == ground_truth_pages[i]["phrases"][phrase["phrase_idx"]]["end"]
                    ), f"Phrase {phrase['phrase_idx']} end time do not match ground truth"
                    assert (
                        phrase["parsed"]
                        == ground_truth_pages[i]["phrases"][phrase["phrase_idx"]]["parsed"]
                    ), f"Phrase {phrase['phrase_idx']} parsed do not match ground truth"

        # 6. Check the asr matches
        assert spy_transcribe_phrase.call_count == 13, (
            f"Transcribe phrase should be called exactly 13 times, called {spy_transcribe_phrase.call_count} times"
        )

        expected_deepgram = activity.ground_truth_deepgram_matches
        assert expected_deepgram is not None
        for i in range(spy_deepgram_word_alignment.call_count):
            assert len(deepgram_return_values[i]) == len(activity.phrases[i].split()), (
                f"Deepgram match does not match phrase length for index {i}"
            )
            assert deepgram_return_values[i] == expected_deepgram[i], (
                f"Deepgram match does not match ground truth for phrase {i}"
            )

        assert spy_rescoring_kaldi_word_alignment.call_count == 13, (
            f"Resliced Kaldi word alignment should be called exactly 13 times, called {spy_rescoring_kaldi_word_alignment.call_count} times"
        )
        expected_kaldi = activity.ground_truth_resliced_kaldi_matches
        assert expected_kaldi is not None
        for i in range(spy_rescoring_kaldi_word_alignment.call_count):
            assert len(kaldi_return_values[i]) == len(activity.phrases[i].split()), (
                f"Resliced Kaldi match does not match phrase length for index {i}"
            )
            assert kaldi_return_values[i] == expected_kaldi[i], (
                f"Resliced Kaldi match does not match ground truth for phrase {i}"
            )

        assert spy_rescoring_w2v_alignment.call_count == 13, (
            f"Resliced W2V word alignment should be called exactly 13 times, called {spy_rescoring_w2v_alignment.call_count} times"
        )
        expected_w2v = activity.ground_truth_resliced_w2v_matches
        assert expected_w2v is not None
        for i in range(spy_rescoring_w2v_alignment.call_count):
            assert len(w2v_return_values[i]) == len(activity.phrases[i].split()), (
                f"Resliced W2V match does not match phrase length for index {i}"
            )
            assert w2v_return_values[i] == expected_w2v[i], (
                f"Resliced W2V match does not match ground truth for phrase {i}"
            )

        # 7. Check the flagging analyzer
        for i in range(spy_flagging_analyzer.call_count):
            assert flagging_analyzer_return_values[i].strategy == FlaggingStrategy.RESCORE_PHRASE, (
                f"Flagging analyzer decision does not match ground truth for phrase {i}"
            )

        # 8. Check final trimmed retouched errors
        expected_retouched = activity.ground_truth_retouched_errors
        assert expected_retouched is not None
        for i in range(len(activity.errors_retouched)):
            assert len(activity.errors_retouched[i]) == len(expected_retouched[i]), (
                f"Retouched errors length do not match ground truth for phrase {i}"
            )
            assert activity.errors_retouched[i] == expected_retouched[i], (
                f"Retouched errors do not match ground truth for phrase {i}"
            )

    async def test_bad_rescore_needs_redo(
        self: TestProcessorIntegration,
        mocker: MockerFixture,
        real_activities: dict[str, Activity],
        real_model_features: dict[str, list[dict[str, Any]]],
        real_deepgram_transcripts: dict[str, dict[str, Any]],
        real_w2v_transcripts: dict[str, list[str]],
        real_kaldi_transcripts: dict[str, list[str]],
        real_manifest_pages: dict[str, list[Any]],
        mock_audio_data: bytes,
        real_timing_files: dict[str, dict[str, Any]],
    ) -> None:
        """Test processing of Jane Goodall passage where earlier iteration performed a bad rescore."""

        activity = real_activities["jane_goodall_bad_needs_rescore"]

        # Load real data for this activity
        deepgram_data = real_deepgram_transcripts[activity.activity_id]

        # Mock all external dependencies with MagicMock
        mock_appsync = MagicMock()
        mock_env = MagicMock()
        mock_env.audio_bucket = "test-bucket"

        # Mock Deepgram client with real transcript
        mock_deepgram = MagicMock()
        transcript_text = deepgram_data["transcript"]
        words = [
            WordItem(
                word=w["word"],
                start=w["start"],
                end=w["end"],
                confidence=w["confidence"],
            )
            for w in deepgram_data["words"]
        ]
        mock_deepgram.transcribe.return_value = TranscriptItem(
            transcript=transcript_text, words=words
        )

        mock_phrase_builder = MagicMock()
        mocker.patch(
            "src.orf_rescoring_pipeline.core.processor.PhraseBuilder",
            return_value=mock_phrase_builder,
        )
        mock_phrase_manifest = MagicMock()
        mock_phrase_manifest.generate.return_value = real_manifest_pages[activity.activity_id]
        mocker.patch(
            "src.orf_rescoring_pipeline.core.processor.PhraseManifest",
            return_value=mock_phrase_manifest,
        )

        # Mock the AppSync get_custom_activity_features method that get_model_features calls
        # This method returns a query function, so we need to return a callable that returns our data
        mock_query_function = MagicMock(return_value=real_model_features[activity.activity_id])
        mock_appsync.get_custom_activity_features.return_value = mock_query_function

        mock_kaldi = MagicMock()
        mock_w2v = MagicMock()

        # Create transcript tuples by pairing kaldi and w2v transcripts by phrase index
        kaldi_transcripts = real_kaldi_transcripts[activity.activity_id]
        w2v_transcripts = real_w2v_transcripts[activity.activity_id]
        transcript_tuples = [
            (kaldi_transcripts[i], w2v_transcripts[i]) for i in range(len(kaldi_transcripts))
        ]

        # Mock transcribe_phrase to return the appropriate tuple for each call
        mocker.patch(
            "src.orf_rescoring_pipeline.rules.rescoring.transcribe_phrase",
            side_effect=transcript_tuples,
        )

        # Mock audio loading from S3 - function doesn't return anything, just sets activity.audio_file_data
        def mock_load_audio(*, activity: Any, audio_bucket: str) -> None:
            activity.audio_file_data = mock_audio_data

        mocker.patch(
            "src.orf_rescoring_pipeline.core.processor.load_activity_audio_data_from_s3",
            side_effect=mock_load_audio,
        )

        # SPY on the key functions we want to verify
        spy_load_page_data = mocker.spy(activity, "load_page_data")

        # phrase alignment for deepgram
        spy_process_activity_timing = mocker.spy(processor_module, "process_activity_timing")
        print(spy_process_activity_timing.call_count)

        # Spy on word alignment functions at their import locations
        # For deepgram matches
        (
            spy_deepgram_word_alignment,
            deepgram_return_values,
        ) = create_return_capturing_spy(
            mocker, processor_module, "get_word_level_transcript_alignment"
        )

        spy_transcribe_phrase = mocker.spy(rescoring_module, "transcribe_phrase")
        # For kaldi matches on re-transcription
        (
            spy_rescoring_kaldi_word_alignment,
            kaldi_return_values,
        ) = create_return_capturing_spy(
            mocker, rescoring_module, "get_word_level_transcript_alignment"
        )

        # For w2v matches on re-transcription
        spy_rescoring_w2v_alignment, w2v_return_values = create_return_capturing_spy(
            mocker, rescoring_module, "get_word_level_transcript_alignment_w2v"
        )

        (
            spy_flagging_analyzer,
            flagging_analyzer_return_values,
        ) = create_return_capturing_spy(mocker, FlaggingAnalyzer, "decide_flagging_strategy")

        # Spy on trim_predictions function
        (
            spy_trim_predictions,
            trim_predictions_return_values,
        ) = create_return_capturing_spy(mocker, processor_module, "trim_predictions")

        # Execute the processor
        result = await process_single_activity(
            activity=activity,
            appsync=mock_appsync,
            env=mock_env,
            deepgram=mock_deepgram,
            kaldi=mock_kaldi,
            w2v=mock_w2v,
            debug=False,
            save_files=False,
        )

        # ASSERTIONS
        assert result[0] is True, f"Processing should succeed for {activity.activity_id}"
        assert result[1] == activity.activity_id

        # 2. External API calls
        mock_appsync.get_custom_activity_features.assert_called_once()
        spy_load_page_data.assert_called_once()
        mock_appsync.set_activity_fields.assert_called_once()
        mock_deepgram.transcribe.assert_called_once()

        # 3. Activity-specific assertions
        assert len(activity.phrases) == 13, "This activity has 13 phrases"
        assert len(activity.pages) == 12, "This activity has 12 pages"

        # 4. activity page consistency checks
        phrase_indices_seen: set[int] = set()
        for page in activity.pages:
            for phrase_index in page.phrase_indices:
                assert phrase_index not in phrase_indices_seen, (
                    f"Phrase index {phrase_index} already seen"
                )
                assert all(phrase_index > idx for idx in phrase_indices_seen), (
                    f"Phrase index {phrase_index} is not greater than all previous phrase indices {phrase_indices_seen}"
                )
                phrase_indices_seen.add(phrase_index)

        # 5. Compare phrase alignment at page level to timing file
        ground_truth_pages = real_timing_files[activity.activity_id]["pages"]
        ground_truth_pages = sorted(ground_truth_pages, key=lambda x: x["page_index"])
        for i, page in enumerate(activity.pages):
            assert page.start_time == ground_truth_pages[i]["page_start"], (
                f"Page {i} start time do not match ground truth"
            )
            assert page.end_time == ground_truth_pages[i]["page_end"], (
                f"Page {i} end time do not match ground truth"
            )
            if page.aligned_phrases:
                for phrase in page.aligned_phrases:
                    assert page.phrase_indices == [
                        p["phrase_idx"] for p in ground_truth_pages[i]["phrases"]
                    ], f"Page {i} phrase indices do not match ground truth"
                    # The timing file contains absolute phrase indices, but the activity.pages contains relative indices in each page
                    assert (
                        phrase["start"]
                        == ground_truth_pages[i]["phrases"][phrase["phrase_idx"]]["start"]
                    ), f"Phrase {phrase['phrase_idx']} start time do not match ground truth"
                    assert (
                        phrase["end"]
                        == ground_truth_pages[i]["phrases"][phrase["phrase_idx"]]["end"]
                    ), f"Phrase {phrase['phrase_idx']} end time do not match ground truth"
                    assert (
                        phrase["parsed"]
                        == ground_truth_pages[i]["phrases"][phrase["phrase_idx"]]["parsed"]
                    ), f"Phrase {phrase['phrase_idx']} parsed do not match ground truth"

        # 6. Check the asr matches
        expected_deepgram = activity.ground_truth_deepgram_matches
        assert expected_deepgram is not None
        for i in range(spy_deepgram_word_alignment.call_count):
            assert len(deepgram_return_values[i]) == len(activity.phrases[i].split()), (
                f"Deepgram match does not match phrase length for index {i}"
            )
            assert deepgram_return_values[i] == expected_deepgram[i], (
                f"Deepgram match does not match ground truth for phrase {i}"
            )

        assert spy_transcribe_phrase.call_count == 10, (
            f"Transcribe phrase should be called exactly 10 times, called {spy_transcribe_phrase.call_count} times"
        )
        assert spy_rescoring_kaldi_word_alignment.call_count == 10, (
            f"Resliced Kaldi word alignment should be called exactly 10 times, called {spy_rescoring_kaldi_word_alignment.call_count} times"
        )
        expected_kaldi = activity.ground_truth_resliced_kaldi_matches
        assert expected_kaldi is not None
        for i in range(spy_rescoring_kaldi_word_alignment.call_count):
            assert len(kaldi_return_values[i]) == len(activity.phrases[i].split()), (
                f"Resliced Kaldi match does not match phrase length for index {i}"
            )
            assert kaldi_return_values[i] == expected_kaldi[i], (
                f"Resliced Kaldi match does not match ground truth for phrase {i}"
            )

        assert spy_rescoring_w2v_alignment.call_count == 10, (
            f"Resliced W2V word alignment should be called exactly 10 times, called {spy_rescoring_w2v_alignment.call_count} times"
        )
        expected_w2v = activity.ground_truth_resliced_w2v_matches
        assert expected_w2v is not None
        for i in range(spy_rescoring_w2v_alignment.call_count):
            assert len(w2v_return_values[i]) == len(activity.phrases[i].split()), (
                f"Resliced W2V match does not match phrase length for index {i}"
            )
            assert w2v_return_values[i] == expected_w2v[i], (
                f"Resliced W2V match does not match ground truth for phrase {i}"
            )

        # 7. Check the flagging analyzer
        assert spy_flagging_analyzer.call_count == len(activity.phrases), (
            "Flagging analyzer should be called exactly 13 times"
        )
        for i in range(spy_flagging_analyzer.call_count):
            if i < 11:
                assert (
                    flagging_analyzer_return_values[i].strategy == FlaggingStrategy.RESCORE_PHRASE
                ), f"Flagging analyzer decision does not match ground truth for phrase {i}"
            else:
                assert (
                    flagging_analyzer_return_values[i].strategy == FlaggingStrategy.MARK_ALL_ERRORS
                ), f"Flagging analyzer decision does not match ground truth for phrase {i}"

        # check that all input error arrays matched the phrase length
        for call in spy_flagging_analyzer.call_args_list:
            # Access keyword arguments since function uses keyword-only parameters
            feature = call.kwargs["feature"]
            errors = call.kwargs["errors"]
            assert len(feature.correct_confidences) == len(errors), (
                f"Input error array does not match phrase length for phrase {feature.phrase_index}"
            )

        # 8. Check final trimmed retouched errors
        expected_retouched = activity.ground_truth_retouched_errors
        assert expected_retouched is not None
        assert len(activity.errors_retouched) == len(expected_retouched), (
            f"Retouched errors length do not match ground truth, retouched errors length: {len(activity.errors_retouched)}, "
            f"ground truth length: {len(expected_retouched)}"
        )
        for i in range(len(activity.errors_retouched)):
            assert len(activity.errors_retouched[i]) == len(expected_retouched[i]), (
                f"Retouched errors length do not match ground truth for phrase {i}"
            )
            assert activity.errors_retouched[i] == expected_retouched[i], (
                f"Retouched errors do not match ground truth for phrase {i}"
            )

        # 9. Check trim_predictions function
        assert spy_trim_predictions.call_count == 1, (
            "trim_predictions should be called exactly once"
        )
        for i in range(len(trim_predictions_return_values[0])):
            assert trim_predictions_return_values[0][i] == expected_retouched[i], (
                f"Trim predictions do not match ground truth for phrase {i}"
            )
