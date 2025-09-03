import numpy as np

from src.pipeline.inference.decoder import PhonemeDecoder


def test_decoder_groups_and_separates_segments() -> None:
    decoder = PhonemeDecoder()

    # Tokens: two 't' characters should group; '|' splits segments; 'æ' is a single vowel symbol
    tokens = ["t", "t", "|", "æ"]
    # Confidence for each token
    probs = np.array([0.9, 0.7, 0.0, 0.8], dtype=np.float32)

    transcript = decoder.decode(pred_tokens=tokens, max_probs=probs)

    # Expect grouped 't' -> single 't', then next segment 'æ'
    assert transcript.elements == ["t", "æ"], f"Unexpected elements: {transcript.elements}"
    assert len(transcript.confidences) == 2
    # First is mean of 0.9 and 0.7, second is 0.8
    assert abs(transcript.confidences[0] - ((0.9 + 0.7) / 2)) < 1e-6
    assert abs(transcript.confidences[1] - 0.8) < 1e-6
