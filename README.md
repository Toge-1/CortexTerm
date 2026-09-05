<div align="center">

# CortexTerm

### 具备上下文、记忆、工具编排和权限控制的终端 AI 编程助手

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=for-the-badge)](LICENSE)
[![Runtime Dependencies: 0](https://img.shields.io/badge/runtime_dependencies-0-f97316?style=for-the-badge)](pyproject.toml)
[![Tests: 217 passed](https://img.shields.io/badge/tests-217%20passed-22c55e?style=for-the-badge)](tests/)

**🇨🇳 中文 | 🇺🇸 [English](README.en.md)**

</div>

---

## 🚀 快速开始

要求 Python 3.11 或更高版本。

```bash
# 1. 获取源码
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# 2. 安装 CortexTerm 包
python -m pip install .

# 3. 配置模型和 API
cortexterm --install

# 4. 在任意目录启动
cortexterm
```

`python -m pip install .` 会根据 `pyproject.toml` 安装项目并注册 `cortexterm` 命令。`cortexterm --install` 运行配置向导，负责保存模型/API 配置并创建启动器。

如果需要修改源码并立即看到效果，第二步改用开发模式：

```bash
python -m pip install -e .
```

安装完成后的正常入口就是 `cortexterm`。只有 Python 的 Scripts/bin 目录没有加入 `PATH`、系统提示找不到该命令时，才使用备用方式：

```bash
python -m cortexterm.main --install
python -m cortexterm.main
```

### 配置启动器 PATH

<details>
<summary><strong>Windows</strong></summary>

1. 按 `Win+R`，输入 `sysdm.cpl`。
2. 打开“高级”→“环境变量”。
3. 编辑用户变量中的 `Path`。
4. 添加 `%USERPROFILE%\.cortexterm\bin`。
5. 重启终端，执行 `cortexterm.bat`。

</details>

<details>
<summary><strong>macOS（zsh）</strong></summary>

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
cortexterm
```

</details>

<details>
<summary><strong>Linux（bash）</strong></summary>

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
cortexterm
```

</details>

---

## 🎯 核心功能

- **终端交互界面**：备用屏幕 TUI、面板渲染、滚动和工具卡片折叠。
- **Agent 工具循环**：模型可以连续调用工具、读取结果并继续推理，直到给出最终回复。
- **上下文管理**：控制发送给模型的消息历史，并在接近窗口上限时压缩旧内容。
- **长期记忆**：按作用域保存偏好、约定、架构、命令、环境和决策等信息。
- **会话持久化**：保存与恢复对话，并支持自动保存。
- **权限控制**：工具执行前进行允许、拒绝或询问判断。
- **MCP 集成**：通过配置接入外部 MCP Server，将其能力注册为可调用工具。
- **29 个内置工具**：覆盖文件、命令、代码分析、测试、Git、Web 和开发辅助。

---

## 🛠️ 内置工具

以下 29 个工具来自默认工具注册表；通过 MCP 接入的外部工具不计入其中。

| 类别 | 工具 | 功能 |
|------|------|------|
| 用户交互与记忆 | `ask_user` | 在执行过程中向用户提问 |
| 用户交互与记忆 | `remember` | 写入长期记忆 |
| 文件操作 | `list_files` | 列出目录内容 |
| 文件操作 | `grep_files` | 跨文件正则搜索 |
| 文件操作 | `read_file` | 按行读取文件 |
| 文件操作 | `write_file` | 创建或覆盖文件 |
| 文件操作 | `modify_file` | 修改文件内容 |
| 文件操作 | `edit_file` | 执行结构化文本编辑 |
| 文件操作 | `patch_file` | 应用补丁 |
| 命令执行 | `run_command` | 执行 Shell 命令 |
| 命令执行 | `run_with_debug` | 执行命令并辅助分析错误 |
| Web 与 API | `web_fetch` | 获取网页内容 |
| Web 与 API | `web_search` | 调用搜索接口 |
| Web 与 API | `api_tester` | 测试 HTTP API |
| 任务管理 | `todo_write` | 管理任务列表 |
| Git | `git` | 执行 Git 工作流操作 |
| Notebook | `notebook_edit` | 编辑 Jupyter Notebook |
| 代码分析 | `find_symbols` | 基于 AST 查找符号 |
| 代码分析 | `find_references` | 查找符号引用 |
| 代码分析 | `get_ast_info` | 获取 AST 结构信息 |
| 代码分析 | `multi_edit` | 批量执行多处编辑 |
| 代码分析 | `code_review` | 分析代码质量问题 |
| 可视化 | `file_tree` | 生成目录树 |
| 可视化 | `diff_viewer` | 展示代码差异 |
| 测试 | `test_runner` | 发现并运行测试 |
| 开发辅助 | `db_explorer` | 查询 SQLite 数据库 |
| 开发辅助 | `docker_helper` | 执行 Docker 与 Compose 操作 |
| 治理 | `governance_audit` | 检查工具和配置治理问题 |
| Skills | `load_skill` | 加载领域技能说明 |

---

## ⚙️ 配置

用户级配置保存在 `~/.cortexterm/settings.json`：

```json
{
  "model": "claude-sonnet-4-20250514",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_AUTH_TOKEN": "your-token-here"
  }
}
```

常用环境变量：

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | Anthropic API Key | 无 |
| `ANTHROPIC_AUTH_TOKEN` | 备用认证令牌 | 无 |
| `ANTHROPIC_BASE_URL` | API 地址 | `https://api.anthropic.com` |
| `ANTHROPIC_MODEL` | Anthropic 模型名 | 无 |
| `CORTEXTERM_MODEL` | 覆盖配置文件中的模型 | 无 |
| `CORTEXTERM_MAX_OUTPUT_TOKENS` | 最大输出 token 数 | 服务商默认值 |
| `CORTEXTERM_MAX_RETRIES` | 可重试 API 错误的重试次数 | `4` |
| `CORTEXTERM_REQUEST_TIMEOUT_SECONDS` | 单次请求超时秒数 | `60` |
| `CORTEXTERM_MODEL_MODE` | 设为 `mock` 可无 API 运行 | 无 |
| `CORTEXTERM_RUN_LIVE_API_TESTS` | 设为 `1` 才运行真实 API 测试 | 无 |

---

## 📖 使用

### 斜杠命令

| 命令 | 功能 |
|------|------|
| `/help` | 显示帮助 |
| `/tools` | 列出工具 |
| `/cost` | 显示当前会话费用 |
| `/config` | 显示配置诊断信息 |
| `/context` | 显示上下文窗口使用情况 |
| `/memory` | 显示记忆系统状态 |
| `/exit` | 退出 CortexTerm |

### 快捷键

| 按键 | 功能 |
|------|------|
| `Enter` | 提交输入 |
| `Up` / `Down` | 切换输入历史 |
| `PageUp` / `PageDown` | 滚动对话记录 |
| `Ctrl+C` | 取消当前操作 |
| `Ctrl+U` | 清空输入行 |

---

## 🧪 开发与测试

```bash
git clone https://github.com/Toge-1/CortexTerm.git
cd CortexTerm

# 安装项目和测试依赖，源码修改立即生效
python -m pip install -e ".[dev]"

# 运行本地测试
python -m pytest

# 无需 API 密钥启动
CORTEXTERM_MODEL_MODE=mock cortexterm

# 可选：显式启用真实 API 测试（会发起外部请求）
CORTEXTERM_RUN_LIVE_API_TESTS=1 python -m pytest tests/test_integration.py -k LiveAPI
```

PowerShell 设置临时环境变量时使用：

```powershell
$env:CORTEXTERM_MODEL_MODE = "mock"
cortexterm
```

---

## 📊 项目统计

| 指标 | 值 |
|------|----|
| Python 包文件数 | 89 |
| 包代码行数 | 约 17,000 |
| 固定内置工具 | 29 |
| 运行时第三方依赖 | 0 |
| 本地测试结果 | 217 通过，3 跳过 |

---

## 📄 许可证

本项目使用 MIT 许可证，详见 [LICENSE](LICENSE)。

<div align="center">

**[English documentation](README.en.md)**

</div>
