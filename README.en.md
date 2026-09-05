<div align="center">

# CortexTerm

### A terminal AI coding assistant with context, memory, tool orchestration, and permission controls

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=for-the-badge)](LICENSE)
[![Runtime Dependencies: 0](https://img.shields.io/badge/runtime_dependencies-0-f97316?style=for-the-badge)](pyproject.toml)
[![Tests: 217 passed](https://img.shields.io/badge/tests-217%20passed-22c55e?style=for-the-badge)](tests/)

**🇨🇳 [中文](README.md) | 🇺🇸 English**

</div>

---

## 🚀 Quick Start

Python 3.11 or later is required.

```bash
# 1. Clone the repository
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# 2. Install the CortexTerm package
python -m pip install .

# 3. Configure the model and API
python -m cortexterm.main --install

# 4. Start CortexTerm
python -m cortexterm.main
```

`python -m pip install .` installs the project. `--install` runs CortexTerm's configuration wizard, which saves model/API settings and creates a launcher; it does not install the Python package.

For source development, install in editable mode instead:

```bash
python -m pip install -e .
```

### Launch Commands

| Platform | Universal command | Launcher created by the wizard |
|----------|-------------------|--------------------------------|
| Windows | `python -m cortexterm.main` | `cortexterm.bat` |
| macOS | `python3 -m cortexterm.main` | `cortexterm` |
| Linux | `python3 -m cortexterm.main` | `cortexterm` |

The pip installation also creates a `cortexterm` command. If it is not on your `PATH`, use the module command shown above.

### Add the Launcher to PATH

<details>
<summary><strong>Windows</strong></summary>

1. Press `Win+R` and enter `sysdm.cpl`.
2. Open Advanced → Environment Variables.
3. Edit `Path` under User Variables.
4. Add `%USERPROFILE%\.cortexterm\bin`.
5. Restart the terminal and run `cortexterm.bat`.

</details>

<details>
<summary><strong>macOS (zsh)</strong></summary>

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
cortexterm
```

</details>

<details>
<summary><strong>Linux (bash)</strong></summary>

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
cortexterm
```

</details>

---

## 🎯 Core Features

- **Terminal UI:** alternate-screen TUI, panel rendering, scrolling, and collapsible tool cards.
- **Agent tool loop:** the model can call tools, consume their results, and continue until it produces a final response.
- **Context management:** manages the messages sent to the model and compacts older content near the context limit.
- **Long-term memory:** stores preferences, conventions, architecture, commands, environment facts, and decisions by scope.
- **Session persistence:** saves and restores conversations with autosave support.
- **Permission controls:** allows, denies, or asks before tools execute.
- **MCP integration:** connects configured MCP servers and registers their capabilities as callable tools.
- **29 built-in tools:** file operations, commands, code analysis, testing, Git, web access, and development utilities.

---

## 🛠️ Built-in Tools

These 29 tools come from the default registry. Tools supplied by configured MCP servers are additional.

| Category | Tool | Purpose |
|----------|------|---------|
| Interaction and memory | `ask_user` | Ask the user a question during execution |
| Interaction and memory | `remember` | Write long-term memory |
| Files | `list_files` | List directory contents |
| Files | `grep_files` | Search files with regular expressions |
| Files | `read_file` | Read files by line range |
| Files | `write_file` | Create or overwrite files |
| Files | `modify_file` | Modify file contents |
| Files | `edit_file` | Perform structured text edits |
| Files | `patch_file` | Apply patches |
| Commands | `run_command` | Execute shell commands |
| Commands | `run_with_debug` | Execute commands and assist with error analysis |
| Web and API | `web_fetch` | Fetch web content |
| Web and API | `web_search` | Call a search provider |
| Web and API | `api_tester` | Test HTTP APIs |
| Tasks | `todo_write` | Manage task lists |
| Git | `git` | Perform Git workflow operations |
| Notebook | `notebook_edit` | Edit Jupyter notebooks |
| Code intelligence | `find_symbols` | Find symbols using the AST |
| Code intelligence | `find_references` | Find symbol references |
| Code intelligence | `get_ast_info` | Inspect AST structure |
| Code intelligence | `multi_edit` | Apply multiple edits |
| Code intelligence | `code_review` | Analyze code quality issues |
| Visualization | `file_tree` | Render directory trees |
| Visualization | `diff_viewer` | Display code differences |
| Testing | `test_runner` | Discover and run tests |
| Development | `db_explorer` | Query SQLite databases |
| Development | `docker_helper` | Run Docker and Compose operations |
| Governance | `governance_audit` | Audit tool and configuration governance |
| Skills | `load_skill` | Load domain-specific skill instructions |

---

## ⚙️ Configuration

User-level settings are stored in `~/.cortexterm/settings.json`:

```json
{
  "model": "claude-sonnet-4-20250514",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_AUTH_TOKEN": "your-token-here"
  }
}
```

Common environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key | none |
| `ANTHROPIC_AUTH_TOKEN` | Alternative authentication token | none |
| `ANTHROPIC_BASE_URL` | API base URL | `https://api.anthropic.com` |
| `ANTHROPIC_MODEL` | Anthropic model name | none |
| `CORTEXTERM_MODEL` | Override the configured model | none |
| `CORTEXTERM_MAX_OUTPUT_TOKENS` | Maximum model output tokens | provider default |
| `CORTEXTERM_MAX_RETRIES` | Retry count for retryable API errors | `4` |
| `CORTEXTERM_REQUEST_TIMEOUT_SECONDS` | Per-request timeout in seconds | `60` |
| `CORTEXTERM_MODEL_MODE` | Set to `mock` to run without an API | none |
| `CORTEXTERM_RUN_LIVE_API_TESTS` | Set to `1` to enable live API tests | none |

---

## 📖 Usage

### Slash Commands

| Command | Purpose |
|---------|---------|
| `/help` | Show help |
| `/tools` | List tools |
| `/cost` | Show the current session cost |
| `/config` | Show configuration diagnostics |
| `/context` | Show context-window usage |
| `/memory` | Show memory-system status |
| `/exit` | Exit CortexTerm |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Submit input |
| `Up` / `Down` | Navigate input history |
| `PageUp` / `PageDown` | Scroll the transcript |
| `Ctrl+C` | Cancel the current operation |
| `Ctrl+U` | Clear the input line |

---

## 🧪 Development and Testing

```bash
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# Install the project and test dependencies in editable mode
python -m pip install -e ".[dev]"

# Run local tests
python -m pytest

# Start without an API key
CORTEXTERM_MODEL_MODE=mock python -m cortexterm.main

# Optional: explicitly enable live API tests (makes external requests)
CORTEXTERM_RUN_LIVE_API_TESTS=1 python -m pytest tests/test_integration.py -k LiveAPI
```

In PowerShell, set a temporary environment variable like this:

```powershell
$env:CORTEXTERM_MODEL_MODE = "mock"
python -m cortexterm.main
```

---

## 📊 Project Statistics

| Metric | Value |
|--------|-------|
| Python package files | 89 |
| Package lines of code | approximately 17,000 |
| Fixed built-in tools | 29 |
| Third-party runtime dependencies | 0 |
| Local test result | 217 passed, 3 skipped |

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

<div align="center">

**[中文文档](README.md)**

</div>
