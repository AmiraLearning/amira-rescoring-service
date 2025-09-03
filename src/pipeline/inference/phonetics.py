from dataclasses import dataclass

from .constants import VALID_PHONETIC_ELEMENTS


class TrieNode:
    def __init__(self) -> None:
        self.children: dict[str, TrieNode] = {}
        self.is_end_of_element: bool = False
        self.element_value: str | None = None


@dataclass(frozen=True)
class LongestMatchResult:
    matched_element: str | None = None
    tokens_consumed: int = 0


class PhoneticTrie:
    def __init__(self, *, phonetic_elements: list[str] | None = None) -> None:
        self.root: TrieNode = TrieNode()
        for element in phonetic_elements or VALID_PHONETIC_ELEMENTS:
            self._insert(element=element)

    def _insert(self, *, element: str) -> None:
        node: TrieNode = self.root
        for char in element:
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.is_end_of_element = True
        node.element_value = element

    def find_longest_match(self, *, tokens: list[str], start_index: int) -> LongestMatchResult:
        node: TrieNode = self.root
        longest_match_element: str | None = None
        longest_match_length: int = 0
        current_length: int = 0
        for idx in range(start_index, len(tokens)):
            token_char: str = tokens[idx]
            if token_char in node.children:
                node = node.children[token_char]
                current_length += 1
                if node.is_end_of_element:
                    longest_match_element = node.element_value
                    longest_match_length = current_length
            else:
                break
        return LongestMatchResult(
            matched_element=longest_match_element, tokens_consumed=longest_match_length
        )
