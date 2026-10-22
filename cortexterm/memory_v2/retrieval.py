"""One lexical ranking, bounded excerpts, and explicit recording-time filters."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime


def tokenize(text: str) -> list[str]:
    return [term for term, _, _ in token_spans(text)]


def token_spans(text: str) -> list[tuple[str, int, int]]:
    # Preserve exact symbols AND identifier components; CJK bigrams do not require
    # a language package, unlike whitespace-only tokenization.
    tokens = []
    for match in re.finditer(r"[a-zA-Z0-9_./:-]+|[\u3400-\u9fff]+", text.lower()):
        word, start = match.group(), match.start()
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            tokens.extend((word[i:i + 2], start + i, start + min(i + 2, len(word)))
                          for i in range(max(1, len(word) - 1)))
        else:
            tokens.append((word, start, match.end()))
            tokens.extend((part.group(), start + part.start(), start + part.end())
                          for part in re.finditer(r"[^_./:-]+", word)
                          if len(part.group()) > 1 and part.group() != word)
    return tokens


def bm25(query: str, documents: list[str]) -> list[tuple[int, float]]:
    bags = [Counter(tokenize(doc)) for doc in documents]
    if not bags:
        return []
    terms = set(tokenize(query))
    avg = sum(sum(b.values()) for b in bags) / len(bags) or 1
    frequencies = {t: sum(t in b for b in bags) for t in terms}
    scores = []
    for i, bag in enumerate(bags):
        length = sum(bag.values())
        score = 0.0
        for term in terms:
            tf = bag[term]
            if tf:
                idf = math.log(1 + (len(bags) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / avg))
        if score > 0:
            scores.append((i, score))
    return sorted(scores, key=lambda v: (-v[1], v[0]))


def search_text(record: dict) -> str:
    """Each record contributes exactly one document to BM25."""
    return record["statement"] + " " + " ".join(record["keywords"] + record["entities"])


def excerpt(text: str, query: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    terms = set(tokenize(query))
    hit = next((start for term, start, _ in token_spans(text) if term in terms), 0)
    start = min(max(0, hit - limit // 3), len(text) - limit)
    return ("…" if start else "") + text[start:start + limit] + ("…" if start + limit < len(text) else "")


def parse_time_range(start_time: str | None, end_time: str | None) -> tuple[float, float] | None:
    """Validate an explicit half-open recording-time range; never infer query semantics."""
    if start_time is None and end_time is None:
        return None
    bounds = []
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("start_time and end_time must both be provided as ISO 8601 timestamps with timezone")
        try:
            instant = datetime.fromisoformat(value.strip())
        except ValueError as error:
            raise ValueError(f"{name} must be a valid ISO 8601 timestamp with timezone") from error
        if instant.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone offset, such as +08:00 or Z")
        bounds.append(instant.timestamp())
    if bounds[0] >= bounds[1]:
        raise ValueError("start_time must be earlier than end_time")
    return bounds[0], bounds[1]
