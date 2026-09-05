from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
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
from cortexterm.prompt import build_system_prompt
from cortexterm.tools import create_default_tool_registry


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


def case_id_to_path_name(case_id: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in case_id)


def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def find_repo(repo_cache: Path, repo: str) -> Path | None:
    candidates = [
        repo_cache / repo_slug(repo),
        repo_cache / repo,
        repo_cache / repo.split("/")[-1],
    ]
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def ensure_repo_cache(repo_cache: Path, repo: str, *, auto_fetch: bool) -> Path | None:
    repo_dir = find_repo(repo_cache, repo)
    if repo_dir is not None:
        return repo_dir
    if not auto_fetch:
        return None

    repo_dir = repo_cache / repo_slug(repo)
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        rc, output = run_command(["git", "init"], cwd=repo_dir, timeout=120)
        if rc != 0:
            raise RuntimeError(f"git init failed in {repo_dir}: {output}")
        rc, output = run_command(["git", "remote", "add", "origin", github_url(repo)], cwd=repo_dir, timeout=120)
        if rc != 0:
            raise RuntimeError(f"git remote add failed in {repo_dir}: {output}")
        run_command(["git", "config", "remote.origin.promisor", "true"], cwd=repo_dir, timeout=120)
        run_command(["git", "config", "remote.origin.partialclonefilter", "blob:none"], cwd=repo_dir, timeout=120)
    return repo_dir


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return completed.returncode, output


def run_command_with_input(
    args: list[str],
    input_text: str,
    *,
    cwd: Path,
    timeout: int = 300,
) -> tuple[int, str]:
    completed = subprocess.run(
        args,
        input=input_text,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return completed.returncode, output


def ensure_base_commit(repo_dir: Path, base_commit: str) -> bool:
    rc, _ = run_command(["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=repo_dir)
    return rc == 0


def fetch_base_commit(repo_dir: Path, base_commit: str) -> tuple[bool, str]:
    if ensure_base_commit(repo_dir, base_commit):
        return True, "base commit already present"
    rc, output = run_command(
        ["git", "fetch", "--depth=1", "--filter=blob:none", "origin", base_commit],
        cwd=repo_dir,
        timeout=900,
    )
    if rc != 0:
        return False, output
    return ensure_base_commit(repo_dir, base_commit), output


def is_fetch_error(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            "failed to connect",
            "could not connect",
            "unable to access",
            "could not fetch",
            "network is unreachable",
            "connection timed out",
            "the read operation timed out",
        )
    )


def remove_existing_worktree(repo_dir: Path, worktree_dir: Path) -> None:
    if not worktree_dir.exists():
        return
    rc, _ = run_command(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=repo_dir, timeout=120)
    if rc != 0 and worktree_dir.exists():
        shutil.rmtree(worktree_dir)


def create_worktree(repo_dir: Path, worktree_dir: Path, base_commit: str, *, force: bool) -> tuple[bool, str]:
    if force:
        remove_existing_worktree(repo_dir, worktree_dir)
    elif worktree_dir.exists():
        return False, f"worktree already exists: {worktree_dir}"

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    rc, output = run_command(
        ["git", "worktree", "add", "--detach", str(worktree_dir), base_commit],
        cwd=repo_dir,
        timeout=300,
    )
    return rc == 0, output


def case_test_patch(case: dict[str, Any]) -> str:
    expected = case.get("expected", {})
    raw = case.get("raw", {})
    for candidate in (expected.get("test_patch"), raw.get("test_patch")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def apply_and_commit_test_patch(worktree_dir: Path, test_patch: str) -> dict[str, Any]:
    if not test_patch.strip():
        return {"status": "missing", "applied": False, "output": "case has no test_patch"}

    rc, output = run_command_with_input(
        ["git", "apply", "--whitespace=nowarn"],
        test_patch,
        cwd=worktree_dir,
        timeout=120,
    )
    if rc != 0:
        rc, output = run_command_with_input(
            ["git", "apply", "-3", "--whitespace=nowarn"],
            test_patch,
            cwd=worktree_dir,
            timeout=120,
        )
    if rc != 0:
        return {"status": "failed", "applied": False, "output": output}

    rc, output = run_command(["git", "add", "-A"], cwd=worktree_dir, timeout=120)
    if rc != 0:
        return {"status": "failed", "applied": True, "output": output}

    rc, output = run_command(
        [
            "git",
            "-c",
            "user.name=CortexTerm Eval",
            "-c",
            "user.email=cortexterm-eval@example.invalid",
            "commit",
            "-m",
            "Apply SWE-bench test patch",
        ],
        cwd=worktree_dir,
        timeout=120,
    )
    if rc != 0:
        return {"status": "failed", "applied": True, "output": output}

    rc, commit_hash = run_command(["git", "rev-parse", "HEAD"], cwd=worktree_dir, timeout=30)
    return {
        "status": "applied",
        "applied": True,
        "commit": commit_hash.strip() if rc == 0 else None,
        "output": output,
    }


def auto_allow_prompt(request: dict[str, Any]) -> dict[str, Any]:
    kind = request.get("kind")
    if kind == "edit":
        return {"decision": "allow_all_turn"}
    return {"decision": "allow_once"}


def build_code_edit_prompt(case: dict[str, Any]) -> str:
    expected = case.get("expected", {})
    return (
        "Solve this SWE-bench Verified task in the checked-out repository.\n"
        "Use this exact workflow: inspect the problem statement and referenced code, locate the bug, "
        "make the smallest source-code change, optionally run relevant tests that already exist in the repository, "
        "and stop when the source fix is complete.\n"
        "Do not modify tests, test fixtures, snapshots, generated benchmark files, or files outside this repository. "
        "Only source-code changes count for this benchmark; test changes make the patch invalid.\n"
        "Do not assume hidden SWE-bench evaluation tests are present during patch generation.\n\n"
        f"Instance: {expected.get('instance_id', case.get('id'))}\n"
        f"Repository: {expected.get('repo', '')}\n"
        f"Base commit: {expected.get('base_commit', '')}\n\n"
        "Problem statement:\n"
        f"{case.get('prompt', '')}"
    )


def last_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("content", ""))
    return ""


def agent_error_from_answer(answer: str) -> str | None:
    prefixes = (
        "Model API timeout:",
        "Model API error",
        "Network error",
    )
    stripped = answer.strip()
    if stripped.startswith(prefixes):
        return stripped
    return None


def collect_patch(worktree_dir: Path) -> tuple[str, str]:
    status_result = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(worktree_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    patch_result = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=str(worktree_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    status = "\n".join(part for part in [status_result.stdout.rstrip(), status_result.stderr.rstrip()] if part)
    patch = "\n".join(part for part in [patch_result.stdout.rstrip(), patch_result.stderr.rstrip()] if part)
    return status, patch


def changed_files_from_status(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            path = line[3:].strip()
        elif len(line) > 3 and line[2] == " ":
            path = line[3:].strip()
        elif len(line) > 2 and line[1] == " ":
            path = line[2:].strip()
        else:
            path = line.strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path:
            files.append(path.replace("\\", "/"))
    return files


def is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or normalized.startswith("test/")
        or "/test/" in normalized
    )


def is_temp_path(path: str) -> bool:
    name = Path(path).name.lower()
    return (
        name.startswith("_tmp")
        or name.startswith("_temp")
        or name.startswith("tmp_")
        or name.startswith("temp_")
        or name.endswith(".tmp")
    )


def invalid_patch_reason(status: str) -> str | None:
    files = changed_files_from_status(status)
    if not files:
        return None
    test_files = [path for path in files if is_test_path(path)]
    if test_files:
        return "patch modifies benchmark tests: " + ", ".join(test_files[:8])
    temp_files = [path for path in files if is_temp_path(path)]
    if temp_files:
        return "patch leaves temporary files in the repository: " + ", ".join(temp_files[:8])
    return None


def untracked_temp_files(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip().replace("\\", "/")
        if path and is_temp_path(path):
            files.append(path)
    return files


def cleanup_untracked_temp_files(status: str, worktree_dir: Path) -> list[str]:
    removed: list[str] = []
    for path in untracked_temp_files(status):
        target = (worktree_dir / path).resolve()
        try:
            target.relative_to(worktree_dir.resolve())
        except ValueError:
            continue
        if target.exists() and target.is_file():
            target.unlink()
            removed.append(path)
    return removed


def changed_source_python_files(status: str, worktree_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in changed_files_from_status(status):
        if is_test_path(path) or is_temp_path(path) or not path.endswith(".py"):
            continue
        target = worktree_dir / path
        if target.exists() and target.is_file():
            files.append(target)
    return files


def source_patch_exists(status: str, patch: str) -> bool:
    if not patch.strip():
        return False
    return any(
        not is_test_path(path) and not is_temp_path(path)
        for path in changed_files_from_status(status)
    )


def build_no_patch_feedback(status: str, *, attempt: int, max_attempts: int) -> str:
    return (
        "No source-code patch was produced after your previous attempt. "
        f"This is retry {attempt} of {max_attempts}. "
        "You must now make the smallest concrete source-code edit that addresses the bug. "
        "Do not create temporary files, do not modify tests, and do not keep exploring unless it is absolutely required. "
        "Use the failing test name and problem statement to identify one source file and edit it now.\n\n"
        f"Current git status:\n{status or '(clean)'}"
    )


def build_compile_feedback(output: str, *, attempt: int, max_attempts: int) -> str:
    return (
        "Your previous source-code patch has a Python syntax error. "
        f"This is syntax-fix retry {attempt} of {max_attempts}. "
        "Fix only the syntax/parse error in source files. Do not modify tests or create temporary files.\n\n"
        f"py_compile output:\n{output[-4000:]}"
    )


def run_py_compile(worktree_dir: Path, files: list[Path], *, python_executable: str, timeout: int = 120) -> dict[str, Any]:
    if not files:
        return {"status": "skipped", "returncode": None, "output": "no changed source Python files"}
    relative_files = [str(path.relative_to(worktree_dir)) for path in files]
    try:
        completed = subprocess.run(
            [python_executable, "-m", "py_compile", *relative_files],
            cwd=str(worktree_dir),
            env=build_test_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        output = "\n".join(part for part in [error.stdout or "", error.stderr or ""] if part).strip()
        return {"status": "timeout", "returncode": None, "output": output, "files": relative_files}
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "output": output,
        "files": relative_files,
    }


def django_module_from_test_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    if not normalized.startswith("tests/") or not normalized.endswith(".py"):
        return None
    return normalized[len("tests/") : -len(".py")].replace("/", ".")


def added_test_methods_from_patch(test_patch: str) -> dict[str, list[str]]:
    added: dict[str, list[str]] = {}
    current_path: str | None = None
    for line in test_patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_path = None
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_path = parts[3][2:]
            continue
        if current_path is None or not line.startswith("+") or line.startswith("+++"):
            continue
        stripped = line[1:].lstrip()
        if stripped.startswith("def test_"):
            method = stripped.split("(", 1)[0].removeprefix("def ").strip()
            if method:
                added.setdefault(current_path, []).append(method)
    return added


def find_test_class_for_method(file_path: Path, method_name: str) -> str | None:
    if not file_path.exists():
        return None
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(f"def {method_name}("):
            continue
        method_indent = len(line) - len(stripped)
        for previous in range(index - 1, -1, -1):
            previous_line = lines[previous]
            previous_stripped = previous_line.lstrip()
            previous_indent = len(previous_line) - len(previous_stripped)
            if previous_indent < method_indent and previous_stripped.startswith("class "):
                return previous_stripped.split("(", 1)[0].removeprefix("class ").strip().rstrip(":")
    return None


def infer_django_tests_from_test_patch(case: dict[str, Any], worktree_dir: Path | None) -> list[str]:
    if worktree_dir is None:
        return []
    inferred: list[str] = []
    for path, methods in added_test_methods_from_patch(case_test_patch(case)).items():
        module = django_module_from_test_path(path)
        if module is None:
            continue
        file_path = worktree_dir / path
        for method in methods:
            class_name = find_test_class_for_method(file_path, method)
            if class_name:
                inferred.append(f"{module}.{class_name}.{method}")
            else:
                inferred.append(f"{module}.{method}")
    return inferred


def build_django_test_plan(
    case: dict[str, Any],
    worktree_dir: Path | None = None,
    *,
    include_hidden_test_metadata: bool = False,
) -> dict[str, Any]:
    expected = case.get("expected", {})
    fail_to_pass = coerce_json(expected.get("fail_to_pass", [])) if include_hidden_test_metadata else []
    pass_to_pass = coerce_json(expected.get("pass_to_pass", [])) if include_hidden_test_metadata else []
    if not isinstance(fail_to_pass, list):
        fail_to_pass = []
    if not isinstance(pass_to_pass, list):
        pass_to_pass = []
    django_tests: list[str] = []
    unparsed_fail_to_pass: list[str] = []
    unparsed_pass_to_pass: list[str] = []

    for item in fail_to_pass:
        label = django_test_label(item)
        if label:
            django_tests.append(label)
        else:
            unparsed_fail_to_pass.append(str(item))

    for item in pass_to_pass:
        label = django_test_label(item)
        if label:
            django_tests.append(label)
        else:
            unparsed_pass_to_pass.append(str(item))

    inferred_from_test_patch = (
        [
            label
            for label in infer_django_tests_from_test_patch(case, worktree_dir)
            if label not in django_tests
        ]
        if include_hidden_test_metadata
        else []
    )
    django_tests.extend(inferred_from_test_patch)

    django_test_classes = sorted(
        {
            label.rsplit(".", 1)[0]
            for label in django_tests
            if "." in label and label.rsplit(".", 1)[1].startswith("test")
        }
    )
    return {
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "tests": [*fail_to_pass, *pass_to_pass],
        "django_tests": django_tests,
        "django_test_classes": django_test_classes,
        "inferred_django_tests_from_test_patch": inferred_from_test_patch,
        "unparsed_fail_to_pass": unparsed_fail_to_pass,
        "unparsed_pass_to_pass": unparsed_pass_to_pass,
    }


def classify_test_template(template: str) -> str:
    if "{django_tests}" in template:
        return "django_methods"
    if "{django_test_classes}" in template:
        return "django_classes"
    if "{tests}" in template:
        return "raw_tests"
    return "custom"


def build_test_command(template: str, case: dict[str, Any], worktree_dir: Path | None = None) -> str:
    expected = case.get("expected", {})
    plan = build_django_test_plan(case, worktree_dir)
    return template.format(
        instance_id=expected.get("instance_id", ""),
        repo=expected.get("repo", ""),
        fail_to_pass=" ".join(str(item) for item in plan["fail_to_pass"]),
        pass_to_pass=" ".join(str(item) for item in plan["pass_to_pass"]),
        tests=" ".join(str(item) for item in plan["tests"]),
        django_tests=" ".join(plan["django_tests"]),
        django_test_classes=" ".join(plan["django_test_classes"]),
    )


def django_test_label(raw: Any) -> str | None:
    text = str(raw).strip()
    if not text:
        return None
    if "(" in text and text.endswith(")"):
        name, _, rest = text.partition("(")
        dotted = rest[:-1].strip()
        test_name = name.strip()
        if dotted and test_name.startswith("test"):
            return f"{dotted}.{test_name}"
    if text.startswith("test") and " " not in text:
        return text
    return None


def classify_test_status(returncode: int, output: str) -> str:
    if returncode == 0:
        return "passed"
    lowered = output.lower()
    env_markers = (
        "modulenotfounderror",
        "zoneinfonotfounderror",
        "unicodedecodeerror",
        "django module not found",
        "no module named",
        "can't decode byte",
    )
    if any(marker in lowered for marker in env_markers):
        return "env_error"
    return "failed"


def build_test_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "." + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_shell_command(command: str, *, cwd: Path, timeout: int) -> tuple[str, int | None, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            env=build_test_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        output = "\n".join(part for part in [error.stdout or "", error.stderr or ""] if part).strip()
        return "timeout", None, output
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return classify_test_status(completed.returncode, output), completed.returncode, output


def run_code_edit_case(
    case: dict[str, Any],
    runtime: dict[str, Any],
    *,
    repo_cache: Path,
    worktree_root: Path,
    max_steps: int,
    force: bool,
    keep_worktree: bool,
    test_command_template: str | None,
    test_timeout: int,
    auto_fetch: bool,
    no_patch_retries: int,
    compile_retries: int,
    compile_python: str,
) -> dict[str, Any]:
    started = time.time()
    expected = case.get("expected", {})
    repo = str(expected.get("repo", ""))
    base_commit = str(expected.get("base_commit", ""))
    try:
        repo_dir = ensure_repo_cache(repo_cache, repo, auto_fetch=auto_fetch)
    except Exception as error:  # noqa: BLE001
        return {
            "id": case["id"],
            "category": "code_edit",
            "status": "repo_not_available",
            "error": str(error),
            "duration_seconds": round(time.time() - started, 3),
        }
    if repo_dir is None:
        return {
            "id": case["id"],
            "category": "code_edit",
            "status": "repo_not_available",
            "error": f"clone {repo} into {repo_cache / repo_slug(repo)} first",
            "duration_seconds": round(time.time() - started, 3),
        }

    if not ensure_base_commit(repo_dir, base_commit):
        if auto_fetch:
            fetched, fetch_output = fetch_base_commit(repo_dir, base_commit)
            if not fetched:
                return {
                    "id": case["id"],
                    "category": "code_edit",
                    "status": "fetch_error" if is_fetch_error(fetch_output) else "base_commit_not_available",
                    "error": fetch_output,
                    "duration_seconds": round(time.time() - started, 3),
                }
        else:
            fetch_output = ""
    if not ensure_base_commit(repo_dir, base_commit):
        return {
            "id": case["id"],
            "category": "code_edit",
            "status": "base_commit_not_available",
            "error": fetch_output or f"{base_commit} is not present in {repo_dir}",
            "duration_seconds": round(time.time() - started, 3),
        }

    worktree_dir = worktree_root / case_id_to_path_name(case["id"])
    ok, output = create_worktree(repo_dir, worktree_dir, base_commit, force=force)
    if not ok:
        status_name = "fetch_error" if is_fetch_error(output) else "worktree_error"
        return {
            "id": case["id"],
            "category": "code_edit",
            "status": status_name,
            "error": output,
            "repo_dir": str(repo_dir),
            "worktree_dir": str(worktree_dir),
            "duration_seconds": round(time.time() - started, 3),
        }

    tools = create_default_tool_registry(str(worktree_dir), runtime=runtime)
    permissions = PermissionManager(str(worktree_dir), prompt=auto_allow_prompt)
    model = AnthropicModelAdapter(runtime, tools)
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                str(worktree_dir),
                permissions.get_summary(),
                {
                    "skills": tools.get_skills(),
                    "mcpServers": tools.get_mcp_servers(),
                },
            ),
        },
        {"role": "user", "content": build_code_edit_prompt(case)},
    ]

    try:
        repair_attempts: list[dict[str, Any]] = []
        cleaned_temp_files: list[str] = []

        def _collect_clean_patch() -> tuple[str, str]:
            nonlocal cleaned_temp_files
            current_status, current_patch = collect_patch(worktree_dir)
            removed = cleanup_untracked_temp_files(current_status, worktree_dir)
            if removed:
                cleaned_temp_files.extend(removed)
                current_status, current_patch = collect_patch(worktree_dir)
            return current_status, current_patch

        def _run_turn(label: str) -> None:
            nonlocal messages
            permissions.begin_turn()
            messages = run_agent_turn(
                model=model,
                tools=tools,
                messages=messages,
                cwd=str(worktree_dir),
                permissions=permissions,
                max_steps=max_steps,
            )
            permissions.end_turn()
            turn_status, turn_patch = _collect_clean_patch()
            repair_attempts.append(
                {
                    "label": label,
                    "git_status": turn_status,
                    "patch_line_count": len(turn_patch.splitlines()) if turn_patch else 0,
                    "answer": last_assistant_content(messages),
                }
            )

        _run_turn("initial")
        status, patch = _collect_clean_patch()
        invalid_reason = invalid_patch_reason(status)
        no_patch_retry_count = 0
        while (
            invalid_reason is None
            and not source_patch_exists(status, patch)
            and no_patch_retry_count < no_patch_retries
        ):
            no_patch_retry_count += 1
            messages.append(
                {
                    "role": "user",
                    "content": build_no_patch_feedback(
                        status,
                        attempt=no_patch_retry_count,
                        max_attempts=no_patch_retries,
                    ),
                }
            )
            _run_turn(f"no_patch_retry_{no_patch_retry_count}")
            status, patch = _collect_clean_patch()
            invalid_reason = invalid_patch_reason(status)

        compile_result = run_py_compile(
            worktree_dir,
            changed_source_python_files(status, worktree_dir),
            python_executable=compile_python,
        )
        compile_retry_count = 0
        while (
            invalid_reason is None
            and source_patch_exists(status, patch)
            and compile_result["status"] in {"failed", "timeout"}
            and compile_retry_count < compile_retries
        ):
            compile_retry_count += 1
            messages.append(
                {
                    "role": "user",
                    "content": build_compile_feedback(
                        str(compile_result.get("output", "")),
                        attempt=compile_retry_count,
                        max_attempts=compile_retries,
                    ),
                }
            )
            _run_turn(f"compile_retry_{compile_retry_count}")
            status, patch = _collect_clean_patch()
            invalid_reason = invalid_patch_reason(status)
            compile_result = run_py_compile(
                worktree_dir,
                changed_source_python_files(status, worktree_dir),
                python_executable=compile_python,
            )

        answer = last_assistant_content(messages)
        agent_error = agent_error_from_answer(answer)
        result_status = "invalid_patch" if invalid_reason else ("ok" if patch.strip() else "no_patch")
        if result_status == "no_patch" and agent_error:
            result_status = "agent_error"

        result: dict[str, Any] = {
            "id": case["id"],
            "category": "code_edit",
            "status": result_status,
            "error": invalid_reason or agent_error,
            "repo_dir": str(repo_dir),
            "worktree_dir": str(worktree_dir),
            "git_status": status,
            "patch": patch,
            "patch_line_count": len(patch.splitlines()) if patch else 0,
            "answer": answer,
            "agent_error": agent_error,
            "repair_attempts": repair_attempts,
            "cleaned_temp_files": cleaned_temp_files,
            "no_patch_retries_used": no_patch_retry_count,
            "compile_retries_used": compile_retry_count,
            "compile_status": compile_result["status"],
            "compile_output": str(compile_result.get("output", ""))[-4000:],
            "compile_files": compile_result.get("files", []),
            "test_patch_status": "not_applied_during_generation",
            "test_patch_commit": None,
            "duration_seconds": round(time.time() - started, 3),
        }

        if invalid_reason:
            result.update(
                {
                    "resolved": False,
                    "test_status": "not_run_invalid_patch",
                }
            )
        elif not source_patch_exists(status, patch):
            result.update(
                {
                    "resolved": False,
                    "test_status": "not_run_agent_error" if agent_error else "not_run_no_patch",
                }
            )
        elif compile_result["status"] in {"failed", "timeout"}:
            result.update(
                {
                    "resolved": False,
                    "status": "syntax_error",
                    "test_status": "not_run_syntax_error",
                    "error": str(compile_result.get("output", ""))[-4000:],
                }
            )
        elif test_command_template:
            test_plan = build_django_test_plan(case, worktree_dir)
            command = build_test_command(test_command_template, case, worktree_dir)
            test_status, returncode, test_output = run_shell_command(command, cwd=worktree_dir, timeout=test_timeout)
            result.update(
                {
                    "resolved": test_status == "passed",
                    "test_status": test_status,
                    "test_returncode": returncode,
                    "test_command": command,
                    "test_command_granularity": classify_test_template(test_command_template),
                    "django_tests_count": len(test_plan["django_tests"]),
                    "django_test_classes_count": len(test_plan["django_test_classes"]),
                    "inferred_django_tests_from_test_patch": test_plan["inferred_django_tests_from_test_patch"],
                    "unparsed_fail_to_pass": test_plan["unparsed_fail_to_pass"],
                    "unparsed_pass_to_pass": test_plan["unparsed_pass_to_pass"],
                    "test_output": test_output[-12000:],
                }
            )
        else:
            result.update(
                {
                    "test_status": "not_run",
                    "resolved": None,
                    "error": "patch generated but no --test-command-template was provided",
                }
            )
        return result
    finally:
        try:
            tools.dispose()
        finally:
            if not keep_worktree:
                remove_existing_worktree(repo_dir, worktree_dir)


def filter_cases(cases: list[dict[str, Any]], *, limit: int | None, offset: int) -> list[dict[str, Any]]:
    selected = [case for case in cases if case.get("category") == "code_edit"]
    if offset:
        selected = selected[offset:]
    if limit is not None:
        selected = selected[:limit]
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CortexTerm on SWE-bench-style code_edit cases.")
    parser.add_argument("--cases", default="evals/cases/public_sample_500.jsonl")
    parser.add_argument("--out", default="evals/results/predictions_swebench.jsonl")
    parser.add_argument("--repo-cache", default="evals/repos", help="Local clone cache, e.g. evals/repos/django__django")
    parser.add_argument("--worktree-root", default="evals/worktrees")
    parser.add_argument("--auto-fetch", action="store_true", help="Create/fetch shallow repo cache for missing base commits.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--no-patch-retries", type=int, default=1)
    parser.add_argument("--compile-retries", type=int, default=1)
    parser.add_argument("--compile-python", default=sys.executable)
    parser.add_argument("--model-request-timeout", type=float, default=30.0)
    parser.add_argument("--model-max-retries", type=int, default=1)
    parser.add_argument("--force", action="store_true", help="Remove an existing worktree for the same case first.")
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument(
        "--test-command-template",
        default=None,
        help=(
            "Shell command run after patch generation. Variables: {fail_to_pass}, "
            "{pass_to_pass}, {tests}, {instance_id}, {repo}."
        ),
    )
    parser.add_argument("--test-timeout", type=int, default=900)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    os.environ["CORTEXTERM_REQUEST_TIMEOUT_SECONDS"] = str(args.model_request_timeout)
    os.environ["CORTEXTERM_MAX_RETRIES"] = str(args.model_max_retries)

    runtime = load_runtime_config(ROOT)
    cases = filter_cases(load_jsonl(Path(args.cases)), limit=args.limit, offset=args.offset)
    repo_cache = Path(args.repo_cache).resolve()
    worktree_root = Path(args.worktree_root).resolve()
    out_path = Path(args.out)
    seen: set[str] = set()
    if args.resume and out_path.exists():
        seen = {row["id"] for row in load_jsonl(out_path)}

    print(f"Running {len(cases)} code_edit cases with model={runtime['model']} -> {out_path}")
    for index, case in enumerate(cases, start=1):
        if case["id"] in seen:
            print(f"[{index}/{len(cases)}] skip {case['id']}")
            continue
        print(f"[{index}/{len(cases)}] {case['id']}")
        try:
            prediction = run_code_edit_case(
                case,
                runtime,
                repo_cache=repo_cache,
                worktree_root=worktree_root,
                max_steps=args.max_steps,
                force=args.force,
                keep_worktree=args.keep_worktree,
                test_command_template=args.test_command_template,
                test_timeout=args.test_timeout,
                auto_fetch=args.auto_fetch,
                no_patch_retries=args.no_patch_retries,
                compile_retries=args.compile_retries,
                compile_python=args.compile_python,
            )
        except Exception as error:  # noqa: BLE001
            prediction = {
                "id": case["id"],
                "category": "code_edit",
                "status": "harness_error",
                "error": f"{type(error).__name__}: {error}",
            }
        append_jsonl(out_path, prediction)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
