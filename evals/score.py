from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
    return rows


def normalize_text(text: Any) -> str:
    text = str(text).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


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


def token_f1(prediction: str, expected: str) -> float:
    pred_tokens = normalize_text(prediction).split()
    expected_tokens = normalize_text(expected).split()
    if not pred_tokens or not expected_tokens:
        return 1.0 if pred_tokens == expected_tokens else 0.0
    pred_counts = Counter(pred_tokens)
    expected_counts = Counter(expected_tokens)
    overlap = sum((pred_counts & expected_counts).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(expected_tokens)
    return 2 * precision * recall / (precision + recall)


def numeric_tokens(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", normalize_text(text))


def memory_answer_score(answer: str, expected: str) -> dict[str, Any]:
    answer_norm = normalize_text(answer)
    expected_norm = normalize_text(expected)
    exact = answer_norm == expected_norm
    f1 = token_f1(answer, expected)

    containment = False
    if answer_norm and expected_norm and not exact:
        answer_parts = answer_norm.split()
        expected_parts = expected_norm.split()
        if len(answer_parts) <= 8 and f" {answer_norm} " in f" {expected_norm} ":
            containment = True
        elif len(expected_parts) <= 8 and f" {expected_norm} " in f" {answer_norm} ":
            containment = True

    answer_numbers = numeric_tokens(answer)
    expected_numbers = numeric_tokens(expected)
    numeric_match = bool(answer_numbers) and any(number in expected_numbers for number in answer_numbers)

    score = 1.0 if exact else f1
    if containment:
        score = max(score, 0.9)
    if numeric_match:
        score = max(score, 0.85)

    return {
        "score": score,
        "exact": exact,
        "f1": f1,
        "containment": containment,
        "numeric_match": numeric_match,
    }


def expected_tool_names(case: dict[str, Any]) -> list[str]:
    return [call["toolName"] for call in expected_tool_calls(case)]


def predicted_tool_names(prediction: dict[str, Any]) -> list[str]:
    return [call["toolName"] for call in predicted_tool_calls(prediction)]


def expected_tool_calls(case: dict[str, Any]) -> list[dict[str, Any]]:
    expected = case.get("expected", {})
    calls = coerce_json(expected.get("tool_calls", []))
    normalized_calls: list[dict[str, Any]] = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            for name, input_data in call.items():
                normalized_calls.append(
                    {
                        "toolName": str(name),
                        "input": input_data if isinstance(input_data, dict) else {},
                    }
                )

    steps = coerce_json(expected.get("steps", []))
    if isinstance(steps, list):
        argument_order = tool_argument_order(case)
        for step in steps:
            parsed = parse_step_call(step, argument_order=argument_order)
            if parsed is not None:
                normalized_calls.append(parsed)
    return normalized_calls


def tool_argument_order(case: dict[str, Any]) -> dict[str, list[str]]:
    order_by_tool: dict[str, list[str]] = {}
    definitions = coerce_json(case.get("tool_definitions", []))
    if not isinstance(definitions, list):
        return order_by_tool

    for definition in definitions:
        if not isinstance(definition, dict) or not definition.get("name"):
            continue
        params = definition.get("parameters", {})
        if not isinstance(params, dict):
            continue
        properties = params.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}

        names: list[str] = []
        required = params.get("required", [])
        if isinstance(required, list):
            names.extend(str(name) for name in required)
        names.extend(
            str(name)
            for name, schema in properties.items()
            if schema is not None and str(name) not in names
        )
        order_by_tool[str(definition["name"])] = names
    return order_by_tool


def literal_eval_node(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return ast.unparse(node)


def parse_step_call(step: Any, *, argument_order: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    if not isinstance(step, str):
        return None
    try:
        expression = ast.parse(step, mode="eval").body
    except SyntaxError:
        name = step.split("(", 1)[0].strip()
        return {"toolName": name, "input": {}} if name else None
    if not isinstance(expression, ast.Call):
        return None

    if isinstance(expression.func, ast.Name):
        name = expression.func.id
    elif isinstance(expression.func, ast.Attribute):
        name = expression.func.attr
    else:
        return None

    input_data: dict[str, Any] = {}
    positional_names = (argument_order or {}).get(name, [])
    for index, argument in enumerate(expression.args):
        key = positional_names[index] if index < len(positional_names) else f"_arg{index + 1}"
        input_data[key] = literal_eval_node(argument)

    for keyword in expression.keywords:
        if keyword.arg is None:
            continue
        input_data[keyword.arg] = literal_eval_node(keyword.value)
    return {"toolName": name, "input": input_data}


def predicted_tool_calls(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    calls = prediction.get("tool_calls", [])
    normalized_calls: list[dict[str, Any]] = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("toolName") or call.get("name") or call.get("tool")
            if name:
                input_data = call.get("input") or call.get("arguments") or call.get("args") or {}
                normalized_calls.append(
                    {
                        "toolName": str(name),
                        "input": input_data if isinstance(input_data, dict) else {},
                    }
                )
    return normalized_calls


def normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = " ".join(value.strip().split())
        lowered = stripped.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"none", "null"}:
            return None
        return lowered
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_value(item) for key, item in value.items()}
    return str(value).strip().lower()


def numeric_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return False


def values_equal(predicted: Any, expected: Any) -> bool:
    predicted_norm = normalize_value(predicted)
    expected_norm = normalize_value(expected)
    if predicted_norm == expected_norm:
        return True
    if numeric_equal(predicted_norm, expected_norm):
        return True
    if isinstance(expected, list):
        return any(values_equal(predicted, option) for option in expected)
    return False


def missing_is_allowed(expected: Any) -> bool:
    if expected == "" or expected is None:
        return True
    if isinstance(expected, list):
        return any(item == "" or item is None for item in expected)
    return False


def score_arguments(expected_input: dict[str, Any], predicted_input: dict[str, Any]) -> dict[str, Any]:
    expected_keys = set(expected_input)
    predicted_keys = set(predicted_input)
    if not expected_keys:
        return {
            "score": 1.0 if not predicted_keys else 0.0,
            "matched": 0,
            "expected": 0,
            "extra_keys": sorted(predicted_keys),
            "missing_keys": [],
            "wrong_keys": [],
        }

    matched = 0
    missing_keys: list[str] = []
    wrong_keys: list[str] = []
    for key, expected_value in expected_input.items():
        if key not in predicted_input:
            if missing_is_allowed(expected_value):
                matched += 1
            else:
                missing_keys.append(key)
            continue
        if values_equal(predicted_input[key], expected_value):
            matched += 1
        else:
            wrong_keys.append(key)

    extra_keys = sorted(predicted_keys - expected_keys)
    recall = matched / len(expected_keys)
    precision_denominator = len(expected_keys) + len(extra_keys)
    precision = matched / precision_denominator if precision_denominator else 0.0
    score = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "score": score,
        "matched": matched,
        "expected": len(expected_keys),
        "extra_keys": extra_keys,
        "missing_keys": missing_keys,
        "wrong_keys": wrong_keys,
    }


def score_tool_call_sequence(expected_calls: list[dict[str, Any]], predicted_calls: list[dict[str, Any]]) -> dict[str, Any]:
    expected_names = [call["toolName"] for call in expected_calls]
    predicted_names = [call["toolName"] for call in predicted_calls]
    if not expected_names:
        return {"score": 0.0, "status": "no_expected_tools"}

    name_matches = sum(
        1
        for index, expected_name in enumerate(expected_names)
        if index < len(predicted_names) and predicted_names[index] == expected_name
    )
    name_recall = name_matches / len(expected_names)
    name_precision = name_matches / len(predicted_names) if predicted_names else 0.0
    name_score = (
        2 * name_precision * name_recall / (name_precision + name_recall)
        if name_precision + name_recall
        else 0.0
    )

    argument_results: list[dict[str, Any]] = []
    for index, expected_call in enumerate(expected_calls):
        predicted_call = predicted_calls[index] if index < len(predicted_calls) else None
        if predicted_call is None or predicted_call["toolName"] != expected_call["toolName"]:
            argument_results.append(
                {
                    "toolName": expected_call["toolName"],
                    "score": 0.0,
                    "matched": 0,
                    "expected": len(expected_call["input"]),
                    "extra_keys": [],
                    "missing_keys": sorted(expected_call["input"]),
                    "wrong_keys": [],
                }
            )
            continue
        result = score_arguments(expected_call["input"], predicted_call["input"])
        argument_results.append({"toolName": expected_call["toolName"], **result})

    argument_score = sum(result["score"] for result in argument_results) / len(argument_results)
    exact_match = (
        predicted_names == expected_names
        and all(result["score"] == 1.0 for result in argument_results)
    )
    score = 1.0 if exact_match else (0.4 * name_score + 0.6 * argument_score)
    return {
        "score": score,
        "name_score": name_score,
        "argument_score": argument_score,
        "status": "scored",
        "expected_tools": expected_names,
        "predicted_tools": predicted_names,
        "ordered_exact_match": predicted_names == expected_names,
        "exact_match": exact_match,
        "argument_results": argument_results,
    }


def score_case(case: dict[str, Any], prediction: dict[str, Any] | None, *, missing_as_zero: bool) -> dict[str, Any]:
    if prediction is None:
        return {"score": 0.0 if missing_as_zero else None, "status": "missing_prediction"}
    if prediction.get("status") == "error":
        return {"score": 0.0, "status": "prediction_error", "error": prediction.get("error")}

    category = case.get("category")
    if category == "long_term_memory":
        expected = str(case.get("expected", {}).get("answer", ""))
        answer = str(prediction.get("answer", prediction.get("content", "")))
        result = memory_answer_score(answer, expected)
        return {"status": "scored", **result}

    if category in {"tool_calling", "multi_step_tool_calling"}:
        return score_tool_call_sequence(expected_tool_calls(case), predicted_tool_calls(prediction))

    if category == "code_edit":
        if prediction.get("status") in {
            "repo_not_available",
            "base_commit_not_available",
            "worktree_error",
            "harness_error",
            "fetch_error",
            "env_error",
            "test_patch_error",
            "needs_test_command",
        }:
            return {
                "score": None,
                "status": prediction["status"],
                "metric": case.get("scoring", {}).get("metric", "swebench_resolved"),
                "error": prediction.get("error"),
            }
        if prediction.get("status") == "invalid_patch":
            return {
                "score": 0.0,
                "status": "scored",
                "resolved": False,
                "test_status": prediction.get("test_status"),
                "error": prediction.get("error"),
            }
        if prediction.get("status") == "syntax_error":
            return {
                "score": 0.0,
                "status": "scored",
                "resolved": False,
                "test_status": prediction.get("test_status"),
                "error": prediction.get("error"),
            }
        if prediction.get("status") == "no_patch":
            return {
                "score": 0.0,
                "status": "scored",
                "resolved": False,
                "test_status": prediction.get("test_status"),
                "error": prediction.get("error"),
            }
        if prediction.get("status") == "agent_error":
            return {
                "score": 0.0,
                "status": "scored",
                "resolved": False,
                "test_status": prediction.get("test_status"),
                "error": prediction.get("error"),
            }
        if prediction.get("test_status") == "not_run":
            return {
                "score": None,
                "status": "needs_test_command",
                "metric": case.get("scoring", {}).get("metric", "swebench_resolved"),
                "error": prediction.get("error"),
                "patch_line_count": prediction.get("patch_line_count", 0),
            }
        if prediction.get("test_status") == "env_error":
            return {
                "score": None,
                "status": "env_error",
                "metric": case.get("scoring", {}).get("metric", "swebench_resolved"),
                "error": prediction.get("test_output", prediction.get("error", ""))[-1000:],
            }
        if "resolved" in prediction and prediction.get("resolved") is not None:
            resolved = bool(prediction.get("resolved"))
            return {
                "score": 1.0 if resolved else 0.0,
                "status": "scored",
                "resolved": resolved,
                "test_status": prediction.get("test_status"),
            }
        if prediction.get("test_status") in {"passed", "failed", "error", "timeout"}:
            passed = prediction.get("test_status") == "passed"
            return {
                "score": 1.0 if passed else 0.0,
                "status": "scored",
                "resolved": passed,
                "test_status": prediction.get("test_status"),
            }
        return {
            "score": None,
            "status": "requires_external_harness",
            "metric": case.get("scoring", {}).get("metric", "swebench_resolved"),
        }

    return {"score": None, "status": "unsupported_category"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score CortexTermEval predictions where offline metrics are available.")
    parser.add_argument("--cases", default="evals/cases/public_sample_500.jsonl")
    parser.add_argument("--predictions", required=True, help="JSONL with at least an id field.")
    parser.add_argument("--out", default="evals/results/score_report.json")
    parser.add_argument("--missing-as-zero", action="store_true")
    args = parser.parse_args(argv)

    cases = load_jsonl(Path(args.cases))
    predictions = {row["id"]: row for row in load_jsonl(Path(args.predictions))}

    scored = []
    by_category: dict[str, list[float]] = defaultdict(list)
    statuses = Counter()

    for case in cases:
        result = score_case(case, predictions.get(case["id"]), missing_as_zero=args.missing_as_zero)
        statuses[result["status"]] += 1
        score = result.get("score")
        if isinstance(score, (int, float)):
            by_category[case["category"]].append(float(score))
        scored.append({"id": case["id"], "category": case["category"], **result})

    summary = {
        "total_cases": len(cases),
        "prediction_count": len(predictions),
        "coverage": len(predictions) / len(cases) if cases else 0.0,
        "statuses": dict(statuses),
        "category_scores": {
            category: {
                "count": len(values),
                "average": sum(values) / len(values) if values else None,
            }
            for category, values in sorted(by_category.items())
        },
    }

    report = {"summary": summary, "cases": scored}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
