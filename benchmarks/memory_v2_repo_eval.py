"""Small real-repository regression replay, with pre-existing tests and git snapshots.

Historical notes are constructed cues, not recovered, chronologically verified sessions.
This is a smoke test for coding-agent integration, not a public memory benchmark.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import zipfile

from benchmarks.memory_v2_eval import MeasuredAdapter
from cortexterm.agent_loop import run_agent_turn
from cortexterm.config import load_runtime_config
from cortexterm.memory_v2.model import MemoryModel
from cortexterm.memory_v2.service import MemoryService, TurnRecorder
from cortexterm.tooling import ToolDefinition, ToolRegistry, ToolResult


CASES = [
    {
        "id": "real_compaction_adapter", "target": "485ebb9",
        "source": "cortexterm/anthropic_adapter.py", "test": "tests/test_anthropic_adapter.py",
        "test_name": "test_anthropic_adapter_sends_compaction_summary_as_first_user_turn",
        "task": (
            "Fix compaction-summary conversion in cortexterm/anthropic_adapter.py. "
            "After compaction, a retained suffix may start with assistant tool_use. "
            "The current converter places the older conversation summary in the system prompt, "
            "leaving no user context before that suffix. "
            "Represent marked summaries as user conversation context, including legacy checkpoints "
            "whose internal role is system. Keep the real system prompt separate, preserve tool-use/result "
            "pairs, and enclose summary text in <summary> newline ... newline </summary>. "
            "Use the existing conversion conventions and keep existing tests passing."
        ),
        "history": (
            "Provider-boundary lesson: isCompactionSummary describes the semantic meaning of a checkpoint; "
            "check it before ordinary role dispatch. Internal storage roles need not equal API roles. "
            "The system prompt must contain instructions, not a summary of user/assistant history. "
            "A summary replaces old conversational turns, so convert it to a user text block. "
            "Reuse _push_anthropic_message to merge adjacent same-role blocks without losing tool IDs."
        ),
    },
    {
        "id": "real_await_user_batch", "target": "d35d4c4",
        "source": "cortexterm/agent_loop.py", "test": "tests/test_agent_loop.py",
        "test_name": "test_await_user_finishes_sibling_tool_calls_before_returning",
        "task": (
            "Fix batched-tool handling in cortexterm/agent_loop.py. "
            "The model can return A, a tool whose result has awaitUser=True, and C in the same batch. "
            "Currently C is skipped. Finish every sibling tool call in order, record every result with "
            "its original ID, then return the question for the user without another model request. "
            "The awaitUser signal may come from any tool, not just a tool named ask_user. "
            "Preserve existing callbacks and normal tool-loop behavior."
        ),
        "history": (
            "Tool orchestration lesson: a batch is one model response. Store its calls before executing "
            "their results; user input should begin only after the batch has a result for each call. "
            "awaitUser is a property of ToolResult, not a special tool name. Accumulate a pending "
            "question across the batch, then leave the outer model loop after all siblings finish. "
            "Do not let a later ordinary result erase an earlier awaitUser decision."
        ),
    },
]

SYSTEM = """You are repairing an existing Python repository. Read the relevant source, make a small
change with edit_file, and run the visible tests. Hidden regression tests are outside your workspace.
Only the specified source file is editable. Treat historical notes as fallible evidence.
Do not ask questions. Finish the requested implementation."""


def git(*args):
    return subprocess.check_output(["git", *args])


def run_tests(root, test_path, name=None):
    command = [sys.executable, "-m", "pytest", str(test_path), "-q", "-p", "no:cacheprovider"]
    if name:
        command += ["-k", name]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
        return {"passed": result.returncode == 0, "returncode": result.returncode,
                "detail": (result.stdout + result.stderr)[-5000:]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": None, "detail": "test timeout"}


def registry(root, case):
    def resolve(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path.suffix not in {".py", ".md", ".toml"} or not path.is_file():
            raise ValueError("Only repository source/document files may be read")
        return path

    def read(data, _context):
        lines = resolve(data["path"]).read_text(encoding="utf-8").splitlines()
        start = max(1, int(data.get("start", 1)))
        end = min(len(lines), start + 159)
        return ToolResult(ok=True, output=f"Lines {start}-{end} of {len(lines)}\n" +
                          "\n".join(f"{n+1}: {line}" for n, line in enumerate(lines[start-1:end], start-1)))

    def edit(data, _context):
        if data["path"] != case["source"]:
            raise ValueError("Only the specified source module is editable")
        path = resolve(data["path"])
        old, new = data["old"], data["new"]
        content = path.read_text(encoding="utf-8")
        if not old or content.count(old) != 1:
            raise ValueError("old must uniquely match the current file; read it first")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return ToolResult(ok=True, output="Replacement applied")

    def tests(_data, _context):
        result = run_tests(root, case["test"])
        return ToolResult(ok=result["passed"], output=result["detail"])

    def definition(name, description, props, required, execute):
        return ToolDefinition(name=name, description=description,
                              input_schema={"type": "object", "properties": props, "required": required},
                              validator=lambda data: data, run=execute)

    return ToolRegistry([
        definition("read_file", "Read up to 160 numbered source lines; increment start to read more.",
                   {"path": {"type": "string"}, "start": {"type": "integer"}}, ["path"], read),
        definition("edit_file", "Replace one exact, unique old substring with new text; omit line numbers.",
                   {n: {"type": "string"} for n in ["path", "old", "new"]}, ["path", "old", "new"], edit),
        definition("run_tests", f"Run existing visible tests in {case['test']}.", {}, [], tests),
    ])


def evaluate(case, output, runtime):
    case_dir = output / case["id"]
    case_dir.mkdir()
    commit = git("rev-parse", case["target"] + "^").decode().strip()
    target = git("rev-parse", case["target"]).decode().strip()
    snapshot = git("archive", "--format=zip", commit)
    gold_source = git("show", f"{target}:{case['source']}").decode("utf-8")
    hidden_tests = git("show", f"{target}:{case['test']}").decode("utf-8")
    rows = []
    with tempfile.TemporaryDirectory(prefix="cortex-repo-memory-") as temp:
        base = Path(temp)
        root = base / "repo"
        root.mkdir()
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            for item in archive.infolist():
                if not (root / item.filename).resolve().is_relative_to(root):
                    raise ValueError("Unsafe archive path")
            archive.extractall(root)
        hidden = base / "test_hidden_regression.py"
        hidden.write_text(hidden_tests, encoding="utf-8")
        source = root / case["source"]
        original = source.read_text(encoding="utf-8")
        before = run_tests(root, hidden, case["test_name"])
        source.write_text(gold_source, encoding="utf-8")
        after = run_tests(root, hidden, case["test_name"])
        source.write_text(original, encoding="utf-8")
        if before["passed"] or not after["passed"]:
            raise RuntimeError("Regression test is not fail-before/pass-after on the frozen git versions")
        memory = MemoryService(root, base / "memory.sqlite3")
        recorder = TurnRecorder(case["history"], turn_id=case["id"] + "-lesson")
        memory.capture(recorder)
        memory.model = MemoryModel(runtime, max_calls=2)
        memory.process_one()
        extraction = list(memory.model.calls)
        memory.model = None
        modes = ["none", "raw", "hybrid"]
        random.Random(42).shuffle(modes)
        for mode in modes:
            source.write_text(original, encoding="utf-8")
            tools = registry(root, case)
            adapter = MeasuredAdapter(runtime, tools, {"remaining": 8})
            context = "" if mode == "none" else memory.context(case["task"], mode=mode)
            events = []
            started = time.monotonic()
            messages = run_agent_turn(
                model=adapter, tools=tools, cwd=str(root), max_steps=8,
                messages=[{"role": "system", "content": SYSTEM + "\n" + context},
                          {"role": "user", "content": case["task"]}],
                on_tool_result=lambda name, text, error: events.append(
                    {"tool": name, "output": text, "error": error}),
            )
            verdict = run_tests(root, hidden)  # Full historical suite: new regression AND old tests.
            row = {
                "case": case["id"], "mode": mode, **verdict,
                "seconds": round(time.monotonic() - started, 3), "requests": adapter.requests,
                "context": context, "retrieval_trace": memory.last_trace if mode != "none" else {},
                "tool_events": events, "final": messages[-1].get("content", ""),
                "source": source.read_text(encoding="utf-8"),
                "base_commit": commit, "fixed_commit": target,
            }
            rows.append(row)
            (case_dir / f"{mode}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case["id"], mode, "PASS" if verdict["passed"] else "FAIL", row["seconds"], flush=True)
        (case_dir / "provenance.json").write_text(json.dumps({
            "base_commit": commit, "fixed_commit": target, "task": case["task"],
            "history": case["history"], "before": before, "fixed_version": after,
            "hidden_test_file": case["test"], "regression_test": case["test_name"],
            "extraction_calls": extraction,
            "limitation": "Historical lesson is constructed from project knowledge, not a time-verified real session.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory")
    args.output.mkdir(parents=True)
    os.environ["CORTEXTERM_MAX_RETRIES"] = "0"
    runtime = {**load_runtime_config(), "maxOutputTokens": 2400, "requestTimeoutSeconds": 45}
    manifest = {"kind": "real_repository_regression_replay", "model": runtime["model"],
                "source_commit": git("rev-parse", "HEAD").decode().strip(),
                "cases": CASES, "repeats": 1, "max_steps": 8,
                "limitations": ["Two tasks, one attempt per mode", "Constructed historical notes",
                                "No new repository/generalization evidence", "Limited read/edit/test tools"]}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(evaluate, case, args.output, runtime) for case in CASES]
        rows = [row for f in futures for row in f.result()]
    summary = {m: {"passed": sum(r["passed"] for r in rows if r["mode"] == m),
                   "runs": sum(r["mode"] == m for r in rows),
                   "seconds": sum(r["seconds"] for r in rows if r["mode"] == m)}
               for m in ["none", "raw", "hybrid"]}
    (args.output / "report.json").write_text(json.dumps({"manifest": manifest, "summary": summary},
                                                      ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
