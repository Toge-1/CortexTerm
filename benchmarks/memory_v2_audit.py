"""Rejudge stored generated code after an explicitly documented checker correction.

Never asks the model to regenerate an answer and never changes raw run files.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from benchmarks.memory_v2_eval import check


def audit(directory):
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    frozen = (directory / "cases.frozen.json").read_text(encoding="utf-8")
    if hashlib.sha256(frozen.encode()).hexdigest() != report["manifest"]["dataset_sha256"]:
        raise ValueError("Frozen dataset differs from the manifest")
    cases = {c["id"]: c for c in json.loads(frozen)["cases"]}
    rows = [json.loads(line) for line in (directory / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    original = cases["approval_wakeup"]["check"]
    old_assertion = "assert not event.flag and 'decision' not in result"
    new_assertion = "assert not event.flag and result.get('decision') is None"
    if original.count(old_assertion) != 1:
        raise ValueError("Unexpected checker version")
    cases["approval_wakeup"]["check"] = original.replace(old_assertion, new_assertion)
    audit_info = {
        "case": "approval_wakeup", "original_assertion": old_assertion,
        "corrected_assertion": new_assertion,
        "reason": "Both an absent decision and a decision cleared to None prevent the old approval from leaking. "
                  "The task did not require deletion of the dictionary key.",
        "model_regeneration": False,
        "original_checker_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "corrected_checker_sha256": hashlib.sha256(cases["approval_wakeup"]["check"].encode()).hexdigest(),
    }
    corrected = []
    for row in rows:
        if row["case"] == "approval_wakeup":
            with tempfile.TemporaryDirectory(prefix="cortex-checker-audit-") as temp:
                base = Path(temp)
                root = base / "workspace"
                root.mkdir()
                evaluator = base / "evaluator"
                evaluator.mkdir()
                for name, content in row["files"].items():
                    path = (root / name).resolve()
                    if not path.is_relative_to(root):
                        raise ValueError("Unsafe generated file path")
                    path.write_text(content, encoding="utf-8")
                verdict = check(root, cases[row["case"]]["check"], evaluator)
            corrected.append({**row, "original_passed": row["passed"],
                              "original_detail": row["detail"], **verdict, "checker_corrected": True})
        else:
            corrected.append({**row, "original_passed": row["passed"], "checker_corrected": False})
    for mode, summary in report["summary"].items():
        summary["original_passed"] = summary["passed"]
        summary["passed"] = sum(r["passed"] for r in corrected if r["mode"] == mode)
    report["checker_audit"] = audit_info
    for name in ["report.audited.json", "runs.audited.jsonl"]:
        if (directory / name).exists():
            raise ValueError("Audited output already exists; do not overwrite evidence")
    (directory / "report.audited.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (directory / "runs.audited.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in corrected), encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    audit(args.directory)
