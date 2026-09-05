<div align="center">

# CortexTerm / CortexTerm 中文版

### 🌏 Bilingual Terminal AI Coding Assistant / 双语终端 AI 编程助手

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=for-the-badge)](LICENSE)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-f97316?style=for-the-badge)](pyproject.toml)
[![Tests: 217 passed](https://img.shields.io/badge/tests-217%20passed-22c55e?style=for-the-badge)](tests/)

---

**🇺🇸 [English](#english) | 🇨🇳 [中文](#中文)**

---

*A zero-dependency terminal coding agent with context, memory, tools, and permission controls. / 一个具备上下文、记忆、工具编排和权限控制的零依赖终端编程智能体。*

</div>

---

# 🇨🇳 中文

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# 交互式安装（推荐）
python -m cortexterm.main --install
```

### 各平台启动命令

| 平台 | 安装后命令 | 直接运行命令 |
|------|-----------|-------------|
| **Windows** | `cortexterm.bat` | `python -m cortexterm.main` |
| **macOS** | `cortexterm` | `python3 -m cortexterm.main` |
| **Linux** | `cortexterm` | `python3 -m cortexterm.main` |

### 配置 PATH

<details>
<summary><strong>📋 Windows 配置 PATH</strong></summary>

1. 按 `Win+R` 输入 `sysdm.cpl`
2. 高级 → 环境变量
3. 在用户变量中找到 `Path`
4. 添加：`%USERPROFILE%\.cortexterm\bin`
5. 重启终端后使用：`cortexterm.bat`
</details>

<details>
<summary><strong>📋 macOS 配置 PATH (zsh)</strong></summary>

```bash
# 快速添加（macOS 默认 zsh）
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 启动命令
cortexterm
```
</details>

<details>
<summary><strong>📋 Linux 配置 PATH (bash)</strong></summary>

```bash
# 快速添加
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# 启动命令
cortexterm
```
</details>

---

## 🎯 核心特性

- **🖥️ 丰富的终端 UI** — 备用屏幕 TUI，面板、ANSI 样式、平滑滚动
- **🤖 智能代理循环** — 多轮工具使用，自动规划、执行、迭代
- **🛠️ 30+ 内置工具** — 文件 I/O、代码搜索、Shell、Git、测试等
- **🔒 权限系统** — 审批、拒绝、自动允许工具调用
- **💾 会话持久化** — 保存并恢复对话，30 秒自动保存
- **🧠 三级记忆** — 对话 → 会话 → 长期记忆
- **🔌 MCP 集成** — 连接外部模型上下文协议服务器
- **⌨️ 斜杠命令** — `/help`、`/tools`、`/cost`、`/config`、`/context`、`/memory`

---

## 🛠️ 内置工具

### 文件操作
| 工具 | 说明 |
|---|---|
| `list_files` | 列出目录内容 |
| `grep_files` | 跨文件正则搜索 |
| `read_file` | 读取文件（支持行范围） |
| `write_file` | 创建或覆盖文件 |
| `edit_file` / `patch_file` | 文件编辑 |

### 代码智能
| 工具 | 说明 |
|---|---|
| `find_symbols` | AST 符号搜索 |
| `find_references` | 查找符号引用 |
| `code_review` | 代码质量分析 |

### 执行与测试
| 工具 | 说明 |
|---|---|
| `run_command` | 执行 Shell 命令 |
| `test_runner` | 测试发现和执行 |

### DevOps
| 工具 | 说明 |
|---|---|
| `git` | Git 工作流 |
| `docker_helper` | Docker 管理 |
| `db_explorer` | SQLite 数据库探索 |

*完整工具列表见 [英文版文档](#-built-in-tools)*

---

## ⚙️ 配置

### 设置文件

`~/.cortexterm/settings.json`：

```json
{
  "model": "claude-sonnet-4-20250514",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_AUTH_TOKEN": "your-token-here"
  }
}
```

### 项目命名

| 对外对象 | 新名称 |
|---|---|
| GitHub 仓库 | `CortexTerm` |
| Python 发行包 | `cortexterm` |
| Python 导入包 | `cortexterm` |
| 终端命令 | `cortexterm` |
| 用户配置目录 | `~/.cortexterm` |

---

## 🧪 开发

```bash
# 克隆仓库
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# 运行测试
pip install -e ".[dev]"
pytest

# Mock 模式（无需 API 密钥）
CORTEXTERM_MODEL_MODE=mock python -m cortexterm.main

# 可选：显式运行真实 API 测试（会产生外部请求）
CORTEXTERM_RUN_LIVE_API_TESTS=1 pytest tests/test_integration.py -k LiveAPI
```

---

## 📊 项目统计

| 指标 | 值 |
|---|---|
| Python 包文件数 | 89 |
| 包代码行数 | ~17,000 |
| 内置工具 | 30+ |
| 外部依赖 | **0** |
| 本地测试结果 | **217 通过，3 跳过** |

---

# 🇺🇸 ENGLISH

## 🚀 Quick Start

### Installation

```bash
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# Interactive installer (recommended)
python -m cortexterm.main --install
```

### Cross-Platform Launch Commands

| Platform | After Install | Direct Run |
|----------|--------------|------------|
| **Windows** | `cortexterm.bat` | `python -m cortexterm.main` |
| **macOS** | `cortexterm` | `python3 -m cortexterm.main` |
| **Linux** | `cortexterm` | `python3 -m cortexterm.main` |

### Configure PATH

<details>
<summary><strong>📋 Windows PATH Setup</strong></summary>

1. Press `Win+R`, type `sysdm.cpl`
2. Advanced → Environment Variables
3. Find `Path` in User Variables
4. Add: `%USERPROFILE%\.cortexterm\bin`
5. Restart terminal, then use: `cortexterm.bat`
</details>

<details>
<summary><strong>📋 macOS PATH Setup (zsh)</strong></summary>

```bash
# Quick setup (macOS default zsh)
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# Launch command
cortexterm
```
</details>

<details>
<summary><strong>📋 Linux PATH Setup (bash)</strong></summary>

```bash
# Quick setup
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Launch command
cortexterm
```
</details>

---

## 🎯 Core Features

- **🖥️ Rich Terminal UI** — Alternate-screen TUI with panels, ANSI styling, smooth scrolling
- **🤖 Intelligent Agent Loop** — Multi-turn tool use, auto-plan/execute/iterate
- **🛠️ 30+ Built-in Tools** — File I/O, code search, shell, git, testing, and more
- **🔒 Permission System** — Approve, deny, auto-allow tool calls
- **💾 Session Persistence** — Save & resume conversations, 30s autosave
- **🧠 3-Tier Memory** — Conversation → Session → Long-term memory
- **🔌 MCP Integration** — Connect external Model Context Protocol servers
- **⌨️ Slash Commands** — `/help`, `/tools`, `/cost`, `/config`, `/context`, `/memory`

---

## 🛠️ Built-in Tools

### File Operations
| Tool | Description |
|------|-------------|
| `list_files` | List directory contents with glob |
| `grep_files` | Regex search across files |
| `read_file` | Read file with line ranges |
| `write_file` | Create or overwrite files |
| `edit_file` / `patch_file` | Structured editing and patching |

### Code Intelligence
| Tool | Description |
|------|-------------|
| `find_symbols` | AST-based symbol search (functions, classes) |
| `find_references` | Find all references to a symbol |
| `code_review` | Automated code quality analysis |

### Execution & Testing
| Tool | Description |
|------|-------------|
| `run_command` | Execute shell commands with timeout |
| `test_runner` | Smart test discovery and execution |
| `api_tester` | HTTP API endpoint testing |

### Web & Search
| Tool | Description |
|------|-------------|
| `web_fetch` | Fetch and extract web page content |
| `web_search` | Web search via API |

### DevOps
| Tool | Description |
|------|-------------|
| `git` | Git workflow (status, diff, log, commit) |
| `docker_helper` | Docker & Docker Compose management |
| `db_explorer` | SQLite database exploration & queries |

### Visualization & Misc
| Tool | Description |
|------|-------------|
| `file_tree` | Visual directory tree |
| `diff_viewer` | Rich diff visualization |
| `notebook_edit` | Jupyter notebook editing |
| `todo_write` | Task list management |
| `ask_user` | Prompt user for clarification |
| `load_skill` | Load domain-specific skills |

---

## ⚙️ Configuration

### Settings File

`~/.cortexterm/settings.json`:

```json
{
  "model": "claude-sonnet-4-20250514",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_AUTH_TOKEN": "your-token-here"
  }
}
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key | — |
| `ANTHROPIC_AUTH_TOKEN` | Auth token (alternative) | — |
| `ANTHROPIC_BASE_URL` | API base URL | `https://api.anthropic.com` |
| `ANTHROPIC_MODEL` | Model name | — |
| `CORTEXTERM_MODEL` | Override the configured model | — |
| `CORTEXTERM_MAX_OUTPUT_TOKENS` | Maximum model output tokens | provider default |
| `CORTEXTERM_MAX_RETRIES` | Retry count for retryable API failures | `4` |
| `CORTEXTERM_REQUEST_TIMEOUT_SECONDS` | Per-request timeout | `60` |
| `CORTEXTERM_MODEL_MODE` | Set to `mock` for testing | — |
| `CORTEXTERM_RUN_LIVE_API_TESTS` | Set to `1` to enable opt-in live API tests | — |

### Project naming

The repository, distribution package, import package, and CLI are named `CortexTerm`, `cortexterm`, `cortexterm`, and `cortexterm` respectively. User-level data is stored under `~/.cortexterm`.

---

## 📖 Usage

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/tools` | List all tools |
| `/cost` | Show session cost |
| `/config` | Show configuration diagnostics |
| `/context` | Show context window usage |
| `/memory` | Show memory system status |
| `/exit` | Exit CortexTerm |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Submit input |
| `Up/Down` | Input history |
| `PageUp/PageDown` | Scroll transcript |
| `Ctrl+C` | Cancel operation |
| `Ctrl+U` | Clear input line |

---

## 🧪 Development

```bash
# Clone
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# Run tests
pip install -e ".[dev]"
pytest

# Mock mode (no API key needed)
CORTEXTERM_MODEL_MODE=mock python -m cortexterm.main

# Optional: explicitly enable live API tests (makes external requests)
CORTEXTERM_RUN_LIVE_API_TESTS=1 pytest tests/test_integration.py -k LiveAPI
```

### Project Stats

| Metric | Value |
|--------|-------|
| Python package files | 89 |
| Package lines of code | ~17,000 |
| Built-in tools | 30+ |
| External dependencies | **0** |
| Local test result | **217 passed, 3 skipped** |

---

## 🙏 Acknowledgments

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — Design inspiration
- **All Contributors** — Everyone who contributed to CortexTerm

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

<div align="center">

**🇨🇳 由 [@QUSETIONS](https://github.com/QUSETIONS) 用 ❤️ 制作** | **🇺🇸 Made with ❤️ by [@QUSETIONS](https://github.com/QUSETIONS)**

*轻量终端 AI 编程助手 / Lightweight Terminal AI Coding Assistant*

[⬆ Back to Top](#cortexterm--cortexterm-中文版)

</div>
