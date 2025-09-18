from src.letter_scoring_pipeline.inference.phonetics import PhoneticTrie


def test_trie_longest_match_simple() -> None:
    trie = PhoneticTrie(phonetic_elements=["t", "th", "æ"])  # custom small set
    tokens = list("thæ")
    res = trie.find_longest_match(tokens=tokens, start_index=0)
    assert res.matched_element == "th"
    assert res.tokens_consumed == 2

    res2 = trie.find_longest_match(tokens=tokens, start_index=2)
    assert res2.matched_element == "æ"
    assert res2.tokens_consumed == 1


def test_trie_no_match() -> None:
    trie = PhoneticTrie(phonetic_elements=["a"])
    res = trie.find_longest_match(tokens=list("bbb"), start_index=0)
    assert res.matched_element is None
    assert res.tokens_consumed == 0
