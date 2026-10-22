"""Run a bounded real-model coding-transfer pilot using CortexTerm's actual loop.

python -m benchmarks.memory_v2_eval --output artifacts/memory-v2 --repeats 2
No public-benchmark or general coding-improvement claim is implied by this pilot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from benchmarks.memory_v2_cases import CASES, DISTRACTORS
from cortexterm.agent_loop import run_agent_turn
from cortexterm.anthropic_adapter import AnthropicModelAdapter
from cortexterm.config import load_runtime_config
from cortexterm.context_manager import estimate_tokens
from cortexterm.memory_v2.model import MemoryModel
from cortexterm.memory_v2.service import MemoryService, TurnRecorder
from cortexterm.tooling import ToolDefinition, ToolRegistry, ToolResult


SYSTEM = """You are a coding agent working in a small Python repository.
Read the relevant source using read_file, then implement the user's request using write_file.
Only modify the files that already exist. Do not ask questions; complete the task.
Historical memory, if supplied, is evidence: obey the current request and check applicability.
The external evaluator will run hidden behavioral tests. Do not attempt to access its files.
"""


def registry(root: Path) -> ToolRegistry:
    allowed = {p.name for p in root.iterdir() if p.is_file()}

    def validate(data):
        if data.get("path") not in allowed:
            raise ValueError("Only the listed project files are accessible")
        return data

    def read(data, _context):
        return ToolResult(ok=True, output=(root / data["path"]).read_text(encoding="utf-8"))

    def write(data, _context):
        content = data.get("content")
        if not isinstance(content, str) or len(content) > 20000:
            raise ValueError("content must be at most 20000 characters")
        (root / data["path"]).write_text(content, encoding="utf-8")
        return ToolResult(ok=True, output="File saved.")

    return ToolRegistry([
        ToolDefinition(name="read_file", description="Read a project file.", validator=validate, run=read,
                       input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
        ToolDefinition(name="write_file", description="Replace a project file with complete Python source.",
                       validator=validate, run=write,
                       input_schema={"type": "object", "properties": {
                           "path": {"type": "string"}, "content": {"type": "string"}},
                           "required": ["path", "content"]}),
    ])


class MeasuredAdapter(AnthropicModelAdapter):
    def __init__(self, runtime, tools, budget):
        super().__init__(runtime, tools)
        self.requests = []
        self.budget = budget

    def _request(self, body):
        if self.budget["remaining"] <= 0:
            raise RuntimeError("Evaluation request budget exhausted")
        self.budget["remaining"] -= 1
        start = time.monotonic()
        response = super()._request(body)
        self.requests.append({"seconds": time.monotonic() - start, "usage": response.get("usage", {})})
        return response


def build_history(memory, case, extractor):
    start = 1780000000
    # No target task, code, test, or gold answer is passed to the extractor.
    for n, text in enumerate(case["history"] + DISTRACTORS):
        rec = TurnRecorder("Prior development session", turn_id=f"{case['id']}-history-{n}")
        rec.recorded_at = start + n * 3600
        rec.append({"role": "user", "content": text})
        memory.store.enqueue({
            "id": rec.id, "project": memory.project, "workspace": memory.workspace,
            "session": f"history-{n}", "recorded_at": rec.recorded_at,
            "request": rec.request, "events": rec.events,
        })
    # Extract all history, including distractors; do not use gold retrieval IDs.
    memory.model = extractor
    while memory.process_one():
        pass
    memory.model = None


def check(root, code, check_dir):
    file = check_dir / "check.py"
    file.write_text(
        "import sys\nsys.path.insert(0, sys.argv[1])\n" + code, encoding="utf-8",
    )
    try:
        result = subprocess.run([sys.executable, "-I", str(file), str(root)],
                                capture_output=True, text=True, timeout=8, cwd=root,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return {"passed": result.returncode == 0, "returncode": result.returncode,
                "detail": (result.stdout + result.stderr)[-2500:]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": None, "detail": "timeout"}


def run(output: Path, repeats: int, max_cases: int, modes: list[str]):
    if repeats < 1 or not 1 <= max_cases <= len(CASES) or set(modes) - {"none", "full", "raw", "hybrid"}:
        raise ValueError("Invalid repeat/case/mode selection")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Use a fresh output directory; an existing experiment must not be overwritten")
    runtime = {**load_runtime_config(), "maxOutputTokens": 1800, "requestTimeoutSeconds": 40}
    # Bound retries as well as agent steps. Applies only to this evaluation process.
    os.environ["CORTEXTERM_MAX_RETRIES"] = "0"
    dataset = CASES[:max_cases]
    frozen = json.dumps({"cases": dataset, "distractors": DISTRACTORS}, sort_keys=True, ensure_ascii=False)
    manifest = {
        "kind": "constructed_coding_transfer_pilot", "model": runtime["model"],
        "dataset_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
        "cases": [c["id"] for c in dataset], "repeats": repeats, "modes": modes,
        "max_steps": 5, "memory_budget_estimated_tokens": 2400,
        "judgment": "hidden executable assertions; no LLM judge",
        "limitations": ["Constructed tasks, not SWE-bench", "Small sample",
                        "No temperature/seed guarantee from provider",
                        "Memory-dependent tasks include conventions absent from the current stub"],
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / "cases.frozen.json").write_text(frozen, encoding="utf-8")
    extractor = MemoryModel(runtime, max_calls=80)
    budget = {"remaining": 300}
    rows = []
    for case in dataset:
        with tempfile.TemporaryDirectory(prefix="cortex-memory-eval-") as temp:
            base = Path(temp)
            root = base / "workspace"
            root.mkdir()
            check_dir = base / "evaluator"
            check_dir.mkdir()
            memory = MemoryService(root, base / "memory.sqlite3")
            build_history(memory, case, extractor)
            (output / f"{case['id']}.memories.json").write_text(
                json.dumps(memory.store.records(memory.project, memory.workspace, active_only=False),
                           ensure_ascii=False, indent=2), encoding="utf-8",
            )
            for repeat in range(repeats):
                order = list(modes)
                random.Random(20261020 + repeat + len(rows)).shuffle(order)
                for mode in order:
                    for name, content in case["files"].items():
                        (root / name).write_text(content, encoding="utf-8")
                    context = "" if mode == "none" else memory.context(
                        case["task"], mode="raw" if mode == "raw" else "hybrid",
                    )
                    if mode == "full":
                        context = "Historical sessions in chronological order (verify applicability):\n" + "\n\n".join(
                            memory._turn_record(t)["statement"]
                            for t in memory.store.turns(memory.project, memory.workspace)
                        )
                        if estimate_tokens(context) > 2400:
                            raise ValueError("Full-history baseline exceeds the common budget; do not silently truncate it")
                    tools = registry(root)
                    model = MeasuredAdapter(runtime, tools, budget)
                    events = []
                    start = time.monotonic()
                    try:
                        messages = run_agent_turn(
                            model=model, tools=tools, cwd=str(root), max_steps=5,
                            messages=[{"role": "system", "content": SYSTEM + "\n" + context},
                                      {"role": "user", "content": case["task"] +
                                       "\nProject files: " + ", ".join(case["files"])}],
                            on_tool_result=lambda name, text, error: events.append(
                                {"tool": name, "output": text, "error": error}),
                        )
                        verdict = check(root, case["check"], check_dir)
                        final = messages[-1].get("content", "")
                    except Exception as exc:
                        verdict = {"passed": False, "detail": type(exc).__name__}
                        final = ""
                    row = {
                        "case": case["id"], "category": case["category"], "repeat": repeat,
                        "mode": mode, **verdict, "seconds": round(time.monotonic() - start, 3),
                        "requests": model.requests, "tool_events": events, "final": final,
                        "context": context, "context_tokens": estimate_tokens(context),
                        "retrieval_trace": memory.last_trace if mode not in {"none", "full"} else {},
                        "files": {n: (root / n).read_text(encoding="utf-8") for n in case["files"]},
                        "extraction_status": memory.store.stats(memory.project, memory.workspace),
                    }
                    rows.append(row)
                    with (output / "runs.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(f"{case['id']} repeat={repeat} mode={mode}: "
                          f"{'PASS' if verdict['passed'] else 'FAIL'} ({row['seconds']}s)", flush=True)
    summary = {}
    for mode in modes:
        group = [r for r in rows if r["mode"] == mode]
        summary[mode] = {
            "passed": sum(r["passed"] for r in group), "runs": len(group),
            "seconds": round(sum(r["seconds"] for r in group), 3),
            "model_calls": sum(len(r["requests"]) for r in group),
            "input_tokens": sum(c["usage"].get("input_tokens", 0) for r in group for c in r["requests"]),
            "output_tokens": sum(c["usage"].get("output_tokens", 0) for r in group for c in r["requests"]),
            "cache_read_tokens": sum(c["usage"].get("cache_read_input_tokens", 0) for r in group for c in r["requests"]),
        }
    report = {"manifest": manifest, "summary": summary, "extraction_calls": extractor.calls}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cases", type=int, default=len(CASES))
    parser.add_argument("--modes", nargs="+", choices=["none", "full", "raw", "hybrid"],
                        default=["none", "full", "raw", "hybrid"])
    args = parser.parse_args()
    run(args.output, args.repeats, args.cases, args.modes)
