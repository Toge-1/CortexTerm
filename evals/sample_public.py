from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_AGENT = "cortexterm-public-eval-sampler/0.1"


DEFAULT_COUNTS = {
    "swebench_verified": 100,
    "bfcl_v3": 200,
    "bfcl_multi_turn": 100,
    "longmemeval": 80,
    "agent_memory": 20,
}


@dataclass(frozen=True)
class HfRowsSource:
    name: str
    dataset: str
    config: str
    split: str


SOURCES = {
    "swebench_verified": HfRowsSource(
        name="swebench_verified",
        dataset="princeton-nlp/SWE-bench_Verified",
        config="default",
        split="test",
    ),
    "bfcl_v3": HfRowsSource(
        name="bfcl_v3",
        dataset="teddyyyy123/bfcl_v3",
        config="default",
        split="train",
    ),
    "bfcl_multi_turn": HfRowsSource(
        name="bfcl_multi_turn",
        dataset="fireworks-ai/bfcl_v3_multi_turn_base",
        config="default",
        split="train",
    ),
}


RAW_JSON_SOURCES = {
    "longmemeval": (
        "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
        "longmemeval_s_cleaned.json"
    ),
    "agent_memory_questions": (
        "https://huggingface.co/datasets/kushalicious/agent-memory-benchmark/resolve/main/"
        "eval/questions.json"
    ),
    "agent_memory_conversation": (
        "https://huggingface.co/datasets/kushalicious/agent-memory-benchmark/resolve/main/"
        "data/conversation.json"
    ),
}


def _cache_path(url: str, cache_dir: Path | None) -> Path | None:
    if cache_dir is None:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def _http_json(url: str, *, timeout: int = 60, cache_dir: Path | None = None) -> Any:
    cache_path = _cache_path(url, cache_dir)
    if cache_path is not None and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    request = Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload
        except HTTPError as error:
            last_error = error
            if error.code not in {429, 500, 502, 503, 504}:
                raise
        except URLError as error:
            last_error = error
        time.sleep(min(2**attempt, 30))
    assert last_error is not None
    raise last_error


def _fetch_hf_page(
    source: HfRowsSource,
    *,
    offset: int,
    length: int,
    cache_dir: Path | None,
) -> dict[str, Any]:
    query = urlencode(
        {
            "dataset": source.dataset,
            "config": source.config,
            "split": source.split,
            "offset": offset,
            "length": length,
        }
    )
    url = f"https://datasets-server.huggingface.co/rows?{query}"
    return _http_json(url, cache_dir=cache_dir)


def fetch_hf_window(
    source: HfRowsSource,
    *,
    count: int,
    seed: int,
    page_size: int = 100,
    cache_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    metadata = _fetch_hf_page(source, offset=0, length=1, cache_dir=cache_dir)
    total = int(metadata["num_rows_total"])
    if count > total:
        raise ValueError(f"cannot sample {count} rows from only {total} rows in {source.dataset}")

    rng = random.Random(seed)
    start = rng.randint(0, total - count)
    end = start + count

    rows: list[dict[str, Any]] = []
    offset = (start // page_size) * page_size
    while offset < end:
        payload = _fetch_hf_page(source, offset=offset, length=page_size, cache_dir=cache_dir)
        page = payload.get("rows", [])
        if not page:
            break
        for item in page:
            row_index = int(item["row_idx"])
            if start <= row_index < end:
                rows.append({"row_index": row_index, "row": item["row"]})
        offset += page_size

    if len(rows) != count:
        raise RuntimeError(f"expected {count} rows from {source.dataset}, got {len(rows)}")
    return rows, total, start


def fetch_raw_json(url: str, *, cache_dir: Path | None = None) -> Any:
    return _http_json(url, timeout=120, cache_dir=cache_dir)


def sample_items(items: list[Any], count: int, *, seed: int) -> list[Any]:
    if count > len(items):
        raise ValueError(f"cannot sample {count} rows from only {len(items)} rows")
    rng = random.Random(seed)
    indices = sorted(rng.sample(range(len(items)), count))
    return [items[index] for index in indices]


def coerce_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def truncate_text(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def compact_text(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " ..."


def overlap_tokens(text: Any) -> set[str]:
    normalized = str(text).lower()
    return {
        token
        for token in re.findall(r"[a-z0-9\u4e00-\u9fff]+", normalized)
        if len(token) > 2 and token not in {"the", "and", "for", "with", "that", "this", "you", "your"}
    }


def session_relevance(session: Any, question_tokens: set[str]) -> int:
    if not question_tokens:
        return 0
    if isinstance(session, list):
        text = " ".join(
            str(message.get("content", ""))
            for message in session
            if isinstance(message, dict)
        )
    else:
        text = str(session)
    return len(overlap_tokens(text) & question_tokens)


def format_memory_session(session_id: str, session: Any) -> list[str]:
    lines = [f"## session {session_id}"]
    if not isinstance(session, list):
        lines.append(compact_text(session, 1600))
        return lines

    for message in session:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "unknown"))
        content = message.get("content", "")
        max_chars = 1100 if role == "user" else 420
        lines.append(f"{role}: {compact_text(content, max_chars)}")
    return lines


def pack_context(parts: list[str], max_chars: int) -> str:
    packed: list[str] = []
    used = 0
    for part in parts:
        extra = len(part) + (1 if packed else 0)
        if used + extra <= max_chars:
            packed.append(part)
            used += extra
            continue
        remaining = max_chars - used - (1 if packed else 0)
        if remaining > 80:
            packed.append(compact_text(part, remaining))
        break
    return "\n".join(packed)


def _source_ref(name: str, dataset: str, split: str, row_index: int | str) -> dict[str, Any]:
    return {
        "name": name,
        "dataset": dataset,
        "split": split,
        "row_index": row_index,
    }


def normalize_swebench_verified(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    instance_id = row["instance_id"]
    return {
        "id": f"swebench_verified:{instance_id}",
        "category": "code_edit",
        "capabilities": ["repo_checkout", "file_edit", "test_execution"],
        "source": _source_ref(
            "SWE-bench Verified",
            "princeton-nlp/SWE-bench_Verified",
            "test",
            item["row_index"],
        ),
        "prompt": row["problem_statement"],
        "expected": {
            "instance_id": instance_id,
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "test_patch": row.get("test_patch", ""),
            "fail_to_pass": row.get("FAIL_TO_PASS", []),
            "pass_to_pass": row.get("PASS_TO_PASS", []),
            "difficulty": row.get("difficulty"),
        },
        "scoring": {
            "metric": "swebench_resolved",
            "requires": ["repo_checkout", "patch_application", "test_harness"],
        },
        "raw": {
            "hints_text": row.get("hints_text", ""),
            "version": row.get("version", ""),
            "created_at": row.get("created_at", ""),
        },
    }


def normalize_bfcl_v3(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    case_id = str(row.get("id") or item["row_index"])
    chat_input = coerce_json(row.get("chat_completion_input") or [])
    user_parts = [
        str(message.get("content", ""))
        for message in chat_input
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    instruction_parts = [
        str(message.get("content", ""))
        for message in chat_input
        if isinstance(message, dict) and message.get("role") != "user"
    ]
    prompt = "\n".join(part for part in user_parts if part.strip()).strip()
    if not prompt:
        prompt = truncate_text(coerce_json(row.get("question", "")), 4000)

    return {
        "id": f"bfcl_v3:{case_id}",
        "category": "tool_calling",
        "capabilities": ["tool_selection", "argument_generation"],
        "source": _source_ref(
            "Berkeley Function Calling Leaderboard v3",
            "teddyyyy123/bfcl_v3",
            "train",
            item["row_index"],
        ),
        "prompt": prompt,
        "tool_definitions": coerce_json(row.get("function", [])),
        "expected": {
            "tool_calls": coerce_json(row.get("ground_truth", [])),
            "language": row.get("language", ""),
        },
        "scoring": {
            "metric": "tool_call_exact_or_ast_match",
            "requires": ["tool_name_match", "argument_match"],
        },
        "raw": {
            "benchmark_instruction": "\n\n".join(instruction_parts).strip(),
        },
    }


def normalize_bfcl_multi_turn(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    messages = coerce_json(row.get("messages") or [])
    prompt = "\n".join(
        f"{message.get('role', 'unknown')}: {message.get('content', '')}"
        for message in messages
        if isinstance(message, dict)
    )
    return {
        "id": f"bfcl_multi_turn:{item['row_index']}",
        "category": "multi_step_tool_calling",
        "capabilities": ["tool_selection", "argument_generation", "multi_step_execution"],
        "source": _source_ref(
            "BFCL v3 multi-turn base",
            "fireworks-ai/bfcl_v3_multi_turn_base",
            "train",
            item["row_index"],
        ),
        "prompt": prompt,
        "tool_definitions": coerce_json(row.get("tools", [])),
        "expected": {
            "steps": coerce_json(row.get("ground_truth", [])),
        },
        "scoring": {
            "metric": "ordered_tool_sequence_match",
            "requires": ["step_order_match", "argument_match"],
        },
    }


def _longmem_context(row: dict[str, Any], *, max_chars: int) -> str:
    session_ids = row.get("haystack_session_ids") or []
    sessions = row.get("haystack_sessions") or []
    answer_ids = set(row.get("answer_session_ids") or [])
    question_tokens = overlap_tokens(row.get("question", ""))
    selected: list[tuple[str, Any]] = []

    for session_id, session in zip(session_ids, sessions, strict=False):
        if session_id in answer_ids:
            selected.append((session_id, session))

    distractors = [
        (session_id, session)
        for session_id, session in zip(session_ids, sessions, strict=False)
        if session_id not in answer_ids
    ]
    distractors.sort(key=lambda item: session_relevance(item[1], question_tokens), reverse=True)
    for session_id, session in distractors:
        if len(selected) >= 6:
            break
        selected.append((session_id, session))

    parts: list[str] = []
    for session_id, session in selected:
        parts.extend(format_memory_session(session_id, session))
    return pack_context(parts, max_chars)


def normalize_longmemeval(row: dict[str, Any], row_index: int, *, max_context_chars: int) -> dict[str, Any]:
    question_id = row["question_id"]
    return {
        "id": f"longmemeval:{question_id}",
        "category": "long_term_memory",
        "capabilities": ["memory_recall", "cross_session_context"],
        "source": _source_ref(
            "LongMemEval cleaned",
            "xiaowu0162/longmemeval-cleaned",
            "longmemeval_s_cleaned",
            row_index,
        ),
        "prompt": row["question"],
        "memory_context": _longmem_context(row, max_chars=max_context_chars),
        "expected": {
            "answer": row.get("answer", ""),
            "question_type": row.get("question_type", ""),
            "answer_session_ids": row.get("answer_session_ids", []),
        },
        "scoring": {
            "metric": "answer_exact_or_f1",
            "requires": ["memory_context_injection"],
        },
    }


def normalize_agent_memory(
    question: dict[str, Any],
    row_index: int,
    *,
    conversation: list[dict[str, Any]],
    max_context_chars: int,
) -> dict[str, Any]:
    context = "\n".join(
        f"{turn.get('role', 'unknown')}: {compact_text(turn.get('content', ''), 900)}"
        for turn in conversation
        if isinstance(turn, dict)
    )
    return {
        "id": f"agent_memory:{question['question_id']}",
        "category": "long_term_memory",
        "capabilities": ["memory_write", "memory_recall", "cross_session_context"],
        "source": _source_ref(
            "agent-memory-benchmark",
            "kushalicious/agent-memory-benchmark",
            "test",
            row_index,
        ),
        "prompt": question["question"],
        "memory_context": truncate_text(context, max_context_chars),
        "expected": {
            "answer": question.get("ground_truth", ""),
        },
        "scoring": {
            "metric": "answer_exact_or_f1",
            "requires": ["memory_context_injection"],
        },
    }


def parse_counts(value: str | None) -> dict[str, int]:
    if not value:
        return dict(DEFAULT_COUNTS)

    counts: dict[str, int] = {}
    for part in value.split(","):
        name, sep, raw_count = part.partition("=")
        if not sep:
            raise ValueError(f"invalid count item: {part!r}; expected name=count")
        counts[name.strip()] = int(raw_count)
    return counts


def build_cases(
    *,
    counts: dict[str, int],
    seed: int,
    max_context_chars: int,
    cache_dir: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    manifest_sources: dict[str, Any] = {}

    if counts.get("swebench_verified", 0):
        source = SOURCES["swebench_verified"]
        selected, available, start = fetch_hf_window(source, count=counts["swebench_verified"], seed=seed + 11, cache_dir=cache_dir)
        cases.extend(normalize_swebench_verified(item) for item in selected)
        manifest_sources[source.name] = {"dataset": source.dataset, "split": source.split, "available": available, "sample_start": start}

    if counts.get("bfcl_v3", 0):
        source = SOURCES["bfcl_v3"]
        selected, available, start = fetch_hf_window(source, count=counts["bfcl_v3"], seed=seed + 23, cache_dir=cache_dir)
        cases.extend(normalize_bfcl_v3(item) for item in selected)
        manifest_sources[source.name] = {"dataset": source.dataset, "split": source.split, "available": available, "sample_start": start}

    if counts.get("bfcl_multi_turn", 0):
        source = SOURCES["bfcl_multi_turn"]
        selected, available, start = fetch_hf_window(source, count=counts["bfcl_multi_turn"], seed=seed + 37, cache_dir=cache_dir)
        cases.extend(normalize_bfcl_multi_turn(item) for item in selected)
        manifest_sources[source.name] = {"dataset": source.dataset, "split": source.split, "available": available, "sample_start": start}

    if counts.get("longmemeval", 0):
        rows = fetch_raw_json(RAW_JSON_SOURCES["longmemeval"], cache_dir=cache_dir)
        indexed_rows = [{"row_index": index, "row": row} for index, row in enumerate(rows)]
        selected = sample_items(indexed_rows, counts["longmemeval"], seed=seed + 41)
        cases.extend(
            normalize_longmemeval(item["row"], item["row_index"], max_context_chars=max_context_chars)
            for item in selected
        )
        manifest_sources["longmemeval"] = {
            "dataset": "xiaowu0162/longmemeval-cleaned",
            "split": "longmemeval_s_cleaned",
            "available": len(rows),
        }

    if counts.get("agent_memory", 0):
        questions = fetch_raw_json(RAW_JSON_SOURCES["agent_memory_questions"], cache_dir=cache_dir)
        conversation = fetch_raw_json(RAW_JSON_SOURCES["agent_memory_conversation"], cache_dir=cache_dir)
        indexed_questions = [{"row_index": index, "row": row} for index, row in enumerate(questions)]
        selected = sample_items(indexed_questions, counts["agent_memory"], seed=seed + 53)
        cases.extend(
            normalize_agent_memory(
                item["row"],
                item["row_index"],
                conversation=conversation,
                max_context_chars=max_context_chars,
            )
            for item in selected
        )
        manifest_sources["agent_memory"] = {
            "dataset": "kushalicious/agent-memory-benchmark",
            "split": "test",
            "available": len(questions),
        }

    cases.sort(key=lambda case: case["id"])
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": seed,
        "counts": counts,
        "total_cases": len(cases),
        "sources": manifest_sources,
        "schema_version": 1,
    }
    return cases, manifest


def write_jsonl(path: Path, cases: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv_summary(path: Path, cases: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "category", "source_name", "capabilities", "metric"],
        )
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "source_name": case["source"]["name"],
                    "capabilities": " ".join(case.get("capabilities", [])),
                    "metric": case.get("scoring", {}).get("metric", ""),
                }
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample public benchmark rows into CortexTermEval JSONL.")
    parser.add_argument("--out", default="evals/cases/public_sample_500.jsonl")
    parser.add_argument("--manifest", default="evals/cases/public_sample_500.manifest.json")
    parser.add_argument("--csv-summary", default="evals/cases/public_sample_500.summary.csv")
    parser.add_argument("--seed", type=int, default=20260618)
    parser.add_argument("--counts", default=None, help="Comma list like swebench_verified=100,bfcl_v3=200")
    parser.add_argument("--max-context-chars", type=int, default=12000)
    parser.add_argument("--cache-dir", default="evals/.cache/http")
    args = parser.parse_args(argv)

    counts = parse_counts(args.counts)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    cases, manifest = build_cases(
        counts=counts,
        seed=args.seed,
        max_context_chars=args.max_context_chars,
        cache_dir=cache_dir,
    )

    out_path = Path(args.out)
    manifest_path = Path(args.manifest)
    summary_path = Path(args.csv_summary)

    write_jsonl(out_path, cases)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv_summary(summary_path, cases)

    print(f"Wrote {len(cases)} cases to {out_path}")
    print(f"Wrote manifest to {manifest_path}")
    print(f"Wrote CSV summary to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
