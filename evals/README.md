# CortexTermEval

CortexTermEval is a lightweight public-benchmark sampling harness for CortexTerm.
It converts public benchmark rows into one JSONL schema so CortexTerm can be
evaluated across coding, terminal-like tool use, multi-step function calling,
and long-term memory tasks.

## Default 500-case sample

`sample_public.py` builds this default mix:

| Source | Count | CortexTerm category | What it checks |
| --- | ---: | --- | --- |
| SWE-bench Verified | 100 | `code_edit` | issue understanding, patch generation, test passing |
| BFCL v3 | 200 | `tool_calling` | tool selection and argument generation |
| BFCL multi-turn base | 100 | `multi_step_tool_calling` | ordered multi-step tool calls |
| LongMemEval cleaned | 80 | `long_term_memory` | cross-session memory recall |
| agent-memory-benchmark | 20 | `long_term_memory` | fact extraction and later recall |

The sample is intentionally mixed. A coding agent that only scores tool calling
is not enough; a local coding agent also needs code edits, memory behavior, and
multi-step tool execution.

## Generate the public sample

From the CortexTerm repository root:

```powershell
python evals\sample_public.py
```

For HuggingFace rows sources, the sampler uses a fixed-seed random contiguous
window instead of downloading every row first. This keeps the script small and
avoids rate limits while still making the selected rows reproducible. The
chosen `sample_start` offsets are stored in the manifest.

Outputs:

```text
evals/cases/public_sample_500.jsonl
evals/cases/public_sample_500.manifest.json
evals/cases/public_sample_500.summary.csv
```

Use a smaller smoke sample:

```powershell
python evals\sample_public.py --out evals\cases\smoke.jsonl --manifest evals\cases\smoke.manifest.json --csv-summary evals\cases\smoke.csv --counts swebench_verified=2,bfcl_v3=2,bfcl_multi_turn=2,longmemeval=2,agent_memory=2
```

Use a different sample budget:

```powershell
python evals\sample_public.py --counts swebench_verified=150,bfcl_v3=150,bfcl_multi_turn=100,longmemeval=80,agent_memory=20
```

## Case schema

Each line is one JSON object:

```json
{
  "id": "bfcl_v3:live_simple_0-0-0",
  "category": "tool_calling",
  "capabilities": ["tool_selection", "argument_generation"],
  "source": {
    "name": "Berkeley Function Calling Leaderboard v3",
    "dataset": "teddyyyy123/bfcl_v3",
    "split": "train",
    "row_index": 0
  },
  "prompt": "...",
  "tool_definitions": [],
  "memory_context": "...",
  "expected": {},
  "scoring": {
    "metric": "tool_call_exact_or_ast_match",
    "requires": ["tool_name_match", "argument_match"]
  }
}
```

Fields are present only when useful. For example, SWE-bench cases have repo and
commit metadata; memory cases have `memory_context`; BFCL cases have
`tool_definitions`.

## Scoring

`score.py` supports lightweight offline metrics:

- `long_term_memory`: normalized exact match plus token F1 fallback.
- `tool_calling`: ordered tool-name matching plus argument matching.
- `multi_step_tool_calling`: ordered multi-step tool-name matching plus
  per-step argument matching.
- `code_edit`: scored only when a SWE-bench harness prediction includes a real
  test result; otherwise it reports setup statuses such as `repo_not_available`
  or `needs_test_command`.

Prediction file format:

```json
{"id": "agent_memory:q1", "answer": "Alex"}
{"id": "bfcl_v3:live_simple_0-0-0", "tool_calls": [{"toolName": "get_user_info", "input": {"user_id": 7890}}]}
```

Run scoring:

```powershell
python evals\score.py --predictions evals\results\predictions.jsonl
```

The tool-call score is stricter than name-only matching. Reports include
`name_score`, `argument_score`, `ordered_exact_match`, and `exact_match` for
each tool case.

## Run CortexTerm on sampled cases

`run_cortexterm_eval.py` runs the configured CortexTerm model over directly
supported non-SWE categories:

- `tool_calling`
- `multi_step_tool_calling`
- `long_term_memory`

It intentionally skips `code_edit`; SWE-bench uses `run_swebench_harness.py`
because it requires a local repo checkout, patch generation, and test command.

Run a small preliminary report:

```powershell
python evals\run_cortexterm_eval.py --categories long_term_memory --limit 10 --out evals\results\predictions_30.jsonl
python evals\run_cortexterm_eval.py --categories tool_calling --limit 10 --out evals\results\predictions_30.jsonl
python evals\run_cortexterm_eval.py --categories multi_step_tool_calling --limit 10 --out evals\results\predictions_30.jsonl
python evals\score.py --predictions evals\results\predictions_30.jsonl --out evals\results\score_30.json
```

Run all directly supported non-SWE cases:

```powershell
python evals\run_cortexterm_eval.py --categories long_term_memory,tool_calling,multi_step_tool_calling --limit 400 --out evals\results\predictions_400.jsonl
python evals\score.py --predictions evals\results\predictions_400.jsonl --out evals\results\score_400.json
```

## Run SWE-bench code-edit cases

Prepare a local clone cache first. The default expected path for `django/django`
is:

```text
evals/repos/django__django
```

Then run a small code-edit smoke:

```powershell
python evals\run_swebench_harness.py --limit 1 --auto-fetch --force --keep-worktree --out evals\results\predictions_swebench.jsonl
```

To score resolved/not-resolved, pass a test command template that is valid for
the checked-out repo. This example assumes the active Python environment has
the target repository's test dependencies installed:

```powershell
python evals\run_swebench_harness.py --limit 1 --auto-fetch --force --test-command-template "set PYTHONPATH=.&& python tests\runtests.py --verbosity 2 {django_tests}" --out evals\results\predictions_swebench.jsonl
python evals\score.py --predictions evals\results\predictions_swebench.jsonl --out evals\results\score_swebench.json
```

Supported template variables are `{fail_to_pass}`, `{pass_to_pass}`, `{tests}`,
`{django_tests}`, `{django_test_classes}`, `{instance_id}`, and `{repo}`. If the
repo clone or test command is missing, the harness records that status instead
of producing a fake score. For Django SWE-bench rows, `{django_tests}` is the
strict local default because it runs the exact parsed test methods from
`FAIL_TO_PASS` and `PASS_TO_PASS`. `{django_test_classes}` is only a coarse
fallback for debugging because it runs whole classes and can both over-test and
hide missing method-level coverage.

The harness uses a strict patch policy: changes under `tests/` are marked
`invalid_patch`, and runs with no source patch are marked `no_patch` without
executing tests. The subprocess test environment also sets `PYTHONPATH=.`,
`PYTHONUTF8=1`, and `PYTHONIOENCODING=utf-8` to reduce Windows-specific noise.

For longer SWE-bench runs, the harness has a few repair and stability switches:

- `--no-patch-retries N`: if the agent finishes without a source patch, feed
  that fact back to the model and retry patch generation.
- `--compile-retries N`: run `py_compile` on changed source Python files and
  let the model repair syntax errors before tests run.
- `--model-request-timeout SECONDS` and `--model-max-retries N`: bound slow or
  unstable model requests so one case does not block the whole batch.

The harness also removes untracked temporary files such as `_temp*.py` before
collecting the final patch, while still rejecting tracked temp files.

For SWE-bench rows with a `test_patch`, the harness applies that official test
patch to the worktree and commits it before the agent starts. This makes the
evaluation baseline match SWE-bench semantics: generated predictions only
contain the agent's source-code fix, while newly introduced benchmark tests are
available when the test command runs.

Some Django `FAIL_TO_PASS` / `PASS_TO_PASS` entries include unittest
descriptions instead of executable labels. The harness records unparsed entries
separately, and for tests newly introduced by `test_patch` it infers
`module.Class.test_method` labels from added `def test_*` methods in the patched
worktree. Predictions record those under
`inferred_django_tests_from_test_patch`.

## What is not solved yet

This harness samples public data and defines comparable cases. It now includes
direct runners for BFCL-style tool use, memory QA, and SWE-bench-style code
editing, but the SWE-bench runner still depends on local repo clones and
repo-specific test commands.

Still not solved:

- Permission runner: this still needs a custom local dangerous-command set,
  because public benchmarks do not map cleanly to CortexTerm's permission sandbox.
- Full SWE-bench parity: official SWE-bench Docker images and environment setup
  are not bundled here.

That split is deliberate: public benchmarks give credibility, while small
project-specific cases are still needed for CortexTerm-only features such as path
permissions and shell-command approvals.
