"""Skip near-duplicate finding replies on the same thread."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_MARKDOWN = re.compile(r"<!--.*?-->|[*_`#>]+", re.DOTALL)
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9_./:+-]+")

SIMILARITY_SKIP = 0.90


def normalize_note_text(text: str) -> str:
    raw = _MARKDOWN.sub(" ", text or "")
    return _WS.sub(" ", raw).strip().lower()


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(normalize_note_text(text)))


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    norm = normalize_note_text(text)
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i : i + n] for i in range(len(norm) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def text_similarity(left: str, right: str) -> float:
    a = normalize_note_text(left)
    b = normalize_note_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ratcliff = SequenceMatcher(None, a, b).ratio()
    token = _jaccard(_tokens(left), _tokens(right))
    ngram = _jaccard(_char_ngrams(left), _char_ngrams(right))
    return max(ratcliff, token, ngram)


def should_skip_similar_reply(
    new_body: str,
    last_body: str,
    *,
    threshold: float = SIMILARITY_SKIP,
) -> bool:
    if not (last_body or "").strip():
        return False
    return text_similarity(new_body, last_body) >= threshold
