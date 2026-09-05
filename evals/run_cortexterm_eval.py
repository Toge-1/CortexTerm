from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortexterm.agent_loop import run_agent_turn
from cortexterm.anthropic_adapter import AnthropicModelAdapter
from cortexterm.config import load_runtime_config
from cortexterm.permissions import PermissionManager
from cortexterm.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


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


def normalize_tool_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)
    schema_type = normalized.get("type")
    normalized["type"] = normalize_json_schema_type(schema_type, default="object")

    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["properties"] = {
            str(name): normalize_property_schema(prop)
            for name, prop in properties.items()
        }
    elif normalized.get("type") == "object":
        normalized["properties"] = {}

    required = normalized.get("required")
    if isinstance(required, list):
        normalized["required"] = [str(item) for item in required]
    elif "required" in normalized:
        normalized.pop("required", None)

    return normalized


def normalize_property_schema(schema: Any) -> dict[str, Any]:
    if schema is None:
        return {"type": "string"}
    if not isinstance(schema, dict):
        return {"type": "string", "description": str(schema)}

    normalized = dict(schema)
    schema_type = normalized.get("type")
    normalized["type"] = normalize_json_schema_type(schema_type, default="string")

    if isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {
            str(name): normalize_property_schema(prop)
            for name, prop in normalized["properties"].items()
        }
    if normalized.get("type") == "array":
        if normalized.get("items") is None:
            normalized["items"] = {"type": "string"}
        else:
            normalized["items"] = normalize_property_schema(normalized["items"])
    if isinstance(normalized.get("anyOf"), list):
        normalized["anyOf"] = [normalize_property_schema(item) for item in normalized["anyOf"]]
    if isinstance(normalized.get("oneOf"), list):
        normalized["oneOf"] = [normalize_property_schema(item) for item in normalized["oneOf"]]
    return normalized


def normalize_json_schema_type(schema_type: Any, *, default: str) -> str:
    if not isinstance(schema_type, str) or not schema_type:
        return default
    mapping = {
        "dict": "object",
        "map": "object",
        "object": "object",
        "float": "number",
        "double": "number",
        "number": "number",
        "int": "integer",
        "integer": "integer",
        "str": "string",
        "string": "string",
        "bool": "boolean",
        "boolean": "boolean",
        "list": "array",
        "array": "array",
        "tuple": "array",
    }
    return mapping.get(schema_type.lower(), default)


def safe_tool_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if not cleaned:
        cleaned = "tool"
    if cleaned[0].isdigit():
        cleaned = "tool_" + cleaned
    return cleaned[:64]


def make_fake_tool(definition: dict[str, Any], calls: list[dict[str, Any]], name_map: dict[str, str]) -> ToolDefinition:
    original_name = str(definition.get("name", "tool"))
    tool_name = safe_tool_name(original_name)
    name_map[tool_name] = original_name
    description = str(definition.get("description") or f"Dataset tool {original_name}")
    input_schema = normalize_tool_schema(definition.get("parameters", {}))

    def _validate(input_data: Any) -> Any:
        return input_data if isinstance(input_data, dict) else {}

    def _run(input_data: Any, _context: ToolContext) -> ToolResult:
        calls.append({"toolName": original_name, "input": input_data})
        return ToolResult(ok=True, output=f"{original_name} executed with {json.dumps(input_data, ensure_ascii=False)}")

    return ToolDefinition(
        name=tool_name,
        description=description,
        input_schema=input_schema,
        validator=_validate,
        run=_run,
    )


def make_tool_registry(case: dict[str, Any], calls: list[dict[str, Any]], name_map: dict[str, str]) -> ToolRegistry:
    raw_definitions = coerce_json(case.get("tool_definitions", []))
    if not isinstance(raw_definitions, list):
        raw_definitions = []
    tools = [
        make_fake_tool(definition, calls, name_map)
        for definition in raw_definitions
        if isinstance(definition, dict) and definition.get("name")
    ]
    return ToolRegistry(tools)


def extract_tool_calls(messages: list[dict[str, Any]], name_map: dict[str, str]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant_tool_call":
            continue
        tool_name = str(message.get("toolName", ""))
        calls.append(
            {
                "toolName": name_map.get(tool_name, tool_name),
                "input": message.get("input", {}),
            }
        )
    return calls


def last_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("content", ""))
    return ""


def build_tool_benchmark_prompt(case: dict[str, Any]) -> str:
    prompt = (
        "You are running a function-calling benchmark. Use the provided tools when they match "
        "the user request. Prefer tool calls over prose. Call only the tools needed for the task.\n\n"
        "Benchmark rules:\n"
        "- Execute the user's requested actions directly; do not narrate a plan instead of calling tools.\n"
        "- Preserve the order expressed by words like first, then, after, before, and finally.\n"
        "- Do not call exploratory tools such as pwd, ls, or find unless the user explicitly asks for them "
        "or they are necessary to satisfy the request.\n"
        "- Treat filenames, directories, accounts, and other entities mentioned by the user as valid benchmark fixtures; "
        "do not verify them with extra discovery calls before acting.\n"
        "- Extra discovery or verification calls before/after the requested actions are counted as wrong in this benchmark.\n"
        "- Stop as soon as the requested tool sequence is complete."
    )
    if case.get("category") == "multi_step_tool_calling":
        prompt += (
            "\n- Multi-step tasks require multiple tool calls. Continue calling tools after each "
            "successful tool result until all requested actions are complete."
        )

    benchmark_instruction = str(case.get("raw", {}).get("benchmark_instruction", "")).strip()
    if benchmark_instruction:
        prompt += "\n\nBenchmark instruction:\n" + benchmark_instruction
    return prompt


def run_tool_case(case: dict[str, Any], runtime: dict[str, Any], *, max_steps: int) -> dict[str, Any]:
    executed_calls: list[dict[str, Any]] = []
    name_map: dict[str, str] = {}
    tools = make_tool_registry(case, executed_calls, name_map)
    model = AnthropicModelAdapter(runtime, tools)
    messages = [
        {
            "role": "system",
            "content": build_tool_benchmark_prompt(case),
        },
        {"role": "user", "content": case["prompt"]},
    ]
    started = time.time()
    try:
        next_messages = run_agent_turn(
            model=model,
            tools=tools,
            messages=messages,
            cwd=str(ROOT),
            permissions=None,
            max_steps=max_steps,
        )
        status = "ok"
        error = None
        tool_calls = extract_tool_calls(next_messages, name_map)
        answer = last_assistant_content(next_messages)
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        tool_calls = []
        answer = ""

    return {
        "id": case["id"],
        "category": case["category"],
        "status": status,
        "error": error,
        "duration_seconds": round(time.time() - started, 3),
        "tool_calls": tool_calls,
        "executed_tool_calls": executed_calls,
        "answer": answer,
    }


def run_memory_case(case: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    tools = ToolRegistry([])
    model = AnthropicModelAdapter(runtime, tools)
    messages = [
        {
            "role": "system",
            "content": (
                "Answer the user's question using only the long-term memory context. "
                "Give the shortest correct answer. If the answer is not present, say unknown.\n\n"
                f"Long-term memory context:\n{case.get('memory_context', '')}"
            ),
        },
        {"role": "user", "content": case["prompt"]},
    ]
    started = time.time()
    try:
        step = model.next(messages)
        status = "ok"
        error = None
        answer = step.content
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        answer = ""

    return {
        "id": case["id"],
        "category": case["category"],
        "status": status,
        "error": error,
        "duration_seconds": round(time.time() - started, 3),
        "answer": answer,
    }


def filter_cases(
    cases: list[dict[str, Any]],
    *,
    categories: set[str],
    limit: int | None,
    offset: int,
) -> list[dict[str, Any]]:
    selected = [case for case in cases if case.get("category") in categories]
    if offset:
        selected = selected[offset:]
    if limit is not None:
        selected = selected[:limit]
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CortexTerm over CortexTermEval cases and write predictions JSONL.")
    parser.add_argument("--cases", default="evals/cases/public_sample_500.jsonl")
    parser.add_argument("--out", default="evals/results/predictions.jsonl")
    parser.add_argument(
        "--categories",
        default="tool_calling,multi_step_tool_calling,long_term_memory",
        help="Comma-separated categories. code_edit requires a separate SWE-bench harness.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--resume", action="store_true", help="Skip ids already present in --out.")
    args = parser.parse_args(argv)

    runtime = load_runtime_config(ROOT)
    cases = load_jsonl(Path(args.cases))
    categories = {item.strip() for item in args.categories.split(",") if item.strip()}
    selected = filter_cases(cases, categories=categories, limit=args.limit, offset=args.offset)

    out_path = Path(args.out)
    seen: set[str] = set()
    if args.resume and out_path.exists():
        seen = {row["id"] for row in load_jsonl(out_path)}

    print(f"Running {len(selected)} cases with model={runtime['model']} -> {out_path}")
    for index, case in enumerate(selected, start=1):
        if case["id"] in seen:
            print(f"[{index}/{len(selected)}] skip {case['id']}")
            continue
        print(f"[{index}/{len(selected)}] {case['id']} ({case['category']})")
        if case["category"] in {"tool_calling", "multi_step_tool_calling"}:
            prediction = run_tool_case(case, runtime, max_steps=args.max_steps)
        elif case["category"] == "long_term_memory":
            prediction = run_memory_case(case, runtime)
        else:
            prediction = {
                "id": case["id"],
                "category": case["category"],
                "status": "skipped",
                "error": "category requires external harness",
            }
        append_jsonl(out_path, prediction)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
