"""Inspectable lexical, temporal, optional semantic, and relation retrieval."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timedelta


def tokenize(text: str) -> list[str]:
    # Preserve exact symbols AND identifier components; CJK bigrams do not require
    # a language package, unlike whitespace-only tokenization.
    words = re.findall(r"[a-zA-Z0-9_./:-]+|[\u3400-\u9fff]+", text.lower())
    tokens = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            tokens.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            tokens.append(word)
            tokens.extend(p for p in re.split(r"[_./:-]+", word) if len(p) > 1 and p != word)
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


def rrf(rankings: list[list[str]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, identity in enumerate(dict.fromkeys(ranking), 1):
            scores[identity] = scores.get(identity, 0) + 1 / (60 + rank)
    return sorted(scores.items(), key=lambda v: (-v[1], v[0]))


def time_window(query: str, now: datetime) -> tuple[float, float] | None:
    """Resolve common explicit dates/day ranges. Ambiguous phrases stay unresolved."""
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", query)
    start = None
    if match:
        try:
            start = datetime.fromisoformat(match.group()).replace(tzinfo=now.tzinfo)
        except ValueError:
            return None
    else:
        offset = next((days for pattern, days in (
            (r"前天|day before yesterday", 2), (r"昨天|昨晚|yesterday|last night", 1),
            (r"今天|today", 0),
        ) if re.search(pattern, query, re.I)), None)
        if offset is not None:
            start = (now - timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
    if start is None:
        return None
    end = start + timedelta(days=1)
    if re.search(r"晚上|昨晚|evening|night", query, re.I):
        start = start.replace(hour=18)
    return start.timestamp(), end.timestamp()


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0
    denom = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(x*y for x, y in zip(left, right)) / denom if denom else 0
