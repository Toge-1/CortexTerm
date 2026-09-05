# CortexTerm Agent 运行链路完整说明

这份文档按当前代码实现整理，目标是让读者可以从入口开始，按顺序理解 CortexTerm 的 agent 每次运行时到底发生了什么、每个模块负责什么、模型和工具如何来回交互、权限和 UI 如何介入、哪些模块在主链路里真正执行、哪些模块是扩展/备用框架。

本文不要按文件树阅读，而要按“用户输入如何变成一次 agent turn”的顺序阅读。

---

## 0. 一句话总览

CortexTerm 启动后会加载配置、工具、权限、记忆、skills、MCP，构造 system prompt 和初始 messages；用户输入后，UI 把用户消息加入 messages，然后调用 `run_agent_turn()`；`run_agent_turn()` 循环调用模型 `model.next(messages)`，如果模型返回工具调用，就用 `ToolRegistry.execute()` 执行对应工具，并把工具结果作为 `tool_result` 塞回 messages，再继续问模型；直到模型返回最终 assistant 文本、调用 `ask_user` 等待用户、达到最大步数、或出现错误兜底；UI 通过 callbacks 展示 thinking/tool/progress/assistant，退出时保存 history/session/transcript，并关闭 MCP 子进程等资源。

核心链路如下：

```text
python -m cortexterm.main
  ↓
main.py::main()
  ↓
load_runtime_config()
create_default_tool_registry()
PermissionManager()
MemoryManager()
build_system_prompt()
  ↓
选择运行界面:
  当前默认 TUI: tty_app.py::run_tty_app()
  非交互 stdin: 逐行处理输入
  ↓
用户输入
  ↓
刷新 system prompt
messages.append(user)
  ↓
agent_loop.py::run_agent_turn()
  ↓
model.next(messages)
  ↓
AnthropicModelAdapter.next()
  ↓
POST /v1/messages
  ↓
AgentStep(type="assistant") 或 AgentStep(type="tool_calls")
  ↓
如果 tool_calls:
  ToolRegistry.execute()
    ↓
    tool.validator()
    tool.run()
    permissions / filesystem / subprocess / network / MCP
    ↓
    ToolResult
  ↓
  messages.append(tool_result)
  ↓
  再次 model.next(messages)
  ↓
最终 assistant / ask_user / max_steps / error
```

---

## 1. 先认清分层

CortexTerm 的代码不是一个单层脚本，而是几层叠起来的。

| 层 | 关键文件 | 负责什么 |
|---|---|---|
| 入口装配层 | `cortexterm/main.py` | CLI 参数、配置加载、模型选择、工具注册、权限初始化、UI 路由 |
| Agent 调度层 | `cortexterm/agent_loop.py` | 模型与工具循环：模型输出、工具执行、工具结果回填、继续推理 |
| 模型协议层 | `cortexterm/anthropic_adapter.py` | 把内部消息转成 Anthropic-compatible API 请求，把响应解析成 `AgentStep` |
| 类型层 | `cortexterm/types.py` | `ChatMessage`、`ToolCall`、`AgentStep`、`ModelAdapter` |
| 工具抽象层 | `cortexterm/tooling.py` | `ToolDefinition`、`ToolRegistry`、`ToolResult` |
| 具体工具层 | `cortexterm/tools/*.py` | 文件、命令、搜索、web、git、测试、MCP、记忆等能力 |
| 权限层 | `cortexterm/permissions.py` + `cortexterm/workspace.py` | 路径、命令、编辑审批和持久化权限 |
| Prompt 层 | `cortexterm/prompt.py` | system prompt、行为协议、治理规则、skills/MCP/memory 注入 |
| 上下文层 | `cortexterm/context_manager.py` | token 估算、上下文压缩 |
| 记忆层 | `cortexterm/memory.py` + `tools/remember.py` | 三层长期记忆读取、注入、写入 |
| MCP 层 | `cortexterm/mcp.py` | 启动 MCP server，把 MCP tools/resources/prompts 包装成 CortexTerm 工具 |
| UI 层 | `tty_app.py` + `cortexterm/tui/*.py` | 当前默认 TUI：接收输入、渲染 conversation、工具卡片、权限弹窗、session、鼠标/键盘事件 |
| 持久化层 | `history.py`、`session.py`、`tui/transcript.py` | prompt history、session resume、transcript 保存 |
| 扩展框架 | `hooks.py`、`auto_mode.py`、`poly_commands.py`、`task_tracker.py`、`sub_agents.py` | 部分接入或未来扩展能力 |

最重要的是：**agent 策略不在 UI 里，也不在某个工具里，核心在 `agent_loop.py::run_agent_turn()`。**

---

## 2. 程序入口：`main.py::main()`

入口文件：

```text
cortexterm/main.py
```

运行方式：

```powershell
python -m cortexterm.main
```

`main()` 的第一件事是解析 CLI 参数：

```text
--resume [SESSION_ID]
--list-sessions
--session SESSION_ID
--install
--validate-config
--log-level DEBUG|INFO|WARNING|ERROR
```

这些参数的作用：

| 参数 | 作用 |
|---|---|
| `--resume` | 恢复已有 session。主要由当前 TUI 使用 |
| `--list-sessions` | 列出保存过的 session |
| `--session` | 指定 session ID，目前主链路使用较弱 |
| `--install` | 进入安装器，直接退出 agent 主流程 |
| `--validate-config` | 打印配置诊断，直接退出 agent 主流程 |
| `--log-level` | 设置日志级别 |

之后初始化日志：

```python
from cortexterm.logging_config import setup_logging
setup_logging(level=args.log_level)
```

如果用户传了 `--validate-config`，会调用：

```python
from cortexterm.config import format_config_diagnostic
print(format_config_diagnostic())
return
```

如果用户传了 `--install`，会调用：

```python
from cortexterm.install import main as install_main
install_main()
return
```

也就是说，`--validate-config` 和 `--install` 是启动前短路，不进入 agent。

---

## 3. 管理命令短路：MCP 和 Skills 管理

`main()` 获取当前目录：

```python
cwd = str(Path.cwd())
argv = sys.argv[1:]
```

然后过滤掉 `--xxx` 参数：

```python
management_argv = [a for a in argv if not a.startswith("--")]
```

接着调用：

```python
if maybe_handle_management_command(cwd, management_argv):
    return
```

相关文件：

```text
cortexterm/manage_cli.py
```

这个函数处理非交互管理命令：

```text
cortexterm mcp list [--project]
cortexterm mcp add <name> [--project] [--protocol ...] [--env KEY=VALUE ...] -- <command> [args...]
cortexterm mcp remove <name> [--project]

cortexterm skills list
cortexterm skills add <path-to-skill-or-dir> [--name <name>] [--project]
cortexterm skills remove <name> [--project]
```

如果命中这些命令，会执行管理动作后退出，不进入 agent loop。

---

## 4. Runtime 配置加载：`load_runtime_config()`

配置加载：

```python
runtime = load_runtime_config(cwd)
```

相关文件：

```text
cortexterm/config.py
```

### 4.1 配置文件路径

`config.py` 定义的关键路径：

```python
CORTEXTERM_DIR = Path.home() / ".cortexterm"
CORTEXTERM_SETTINGS_PATH = CORTEXTERM_DIR / "settings.json"
CORTEXTERM_HISTORY_PATH = CORTEXTERM_DIR / "history.json"
CORTEXTERM_PERMISSIONS_PATH = CORTEXTERM_DIR / "permissions.json"
CORTEXTERM_MCP_PATH = CORTEXTERM_DIR / "mcp.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
```

也就是说，它兼容部分 Claude 配置路径，同时使用自己的 `~/.cortexterm` 目录。

### 4.2 配置合并顺序

`load_runtime_config()` 内部先走：

```python
load_effective_settings(cwd)
```

加载并合并：

```text
~/.claude/settings.json
~/.cortexterm/mcp.json
<cwd>/.mcp.json
~/.cortexterm/settings.json
环境变量
Windows 用户/系统环境变量
```

MCP server 配置也会在这里合并。

### 4.3 DeepSeek 特殊处理

如果环境变量或 Windows 环境变量里有：

```text
DEEPSEEK_API_KEY
```

则 runtime 走 DeepSeek Anthropic-compatible 路径：

```python
model = os.environ.get("CORTEXTERM_MODEL") or "deepseek-v4-pro"
base_url = "https://api.deepseek.com/anthropic"
api_key = deepseek_api_key
max_output_tokens = 4096  # 如果没有显式设置
```

否则走 Anthropic 路径：

```python
model = CORTEXTERM_MODEL or settings.model or ANTHROPIC_MODEL
base_url = ANTHROPIC_BASE_URL or "https://api.anthropic.com"
api_key = ANTHROPIC_API_KEY
auth_token = ANTHROPIC_AUTH_TOKEN
```

如果没有模型，会抛错：

```text
No model configured. Set ~/.cortexterm/settings.json or ANTHROPIC_MODEL.
```

### 4.4 配置加载失败时不会直接退出

`main.py` 里包了：

```python
try:
    runtime = load_runtime_config(cwd)
except Exception:
    runtime = None
    print(warning...)
```

如果配置失败，`runtime = None`，后面会使用 `MockModelAdapter()`。

---

## 5. 工具注册：`create_default_tool_registry()`

工具注册在：

```python
tools = create_default_tool_registry(cwd, runtime=runtime)
```

相关文件：

```text
cortexterm/tools/__init__.py
cortexterm/tooling.py
```

### 5.1 工具抽象：`ToolDefinition`

`tooling.py` 定义：

```python
@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    validator: Validator
    run: Runner
```

每个工具由五部分组成：

| 字段 | 含义 |
|---|---|
| `name` | 工具名，模型调用时用这个名字 |
| `description` | 工具说明，会暴露给模型 |
| `input_schema` | JSON schema，也会暴露给模型 |
| `validator` | 执行前校验和规范化输入 |
| `run` | 真正执行工具逻辑 |

工具返回：

```python
@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: str
    backgroundTask: BackgroundTaskResult | None = None
    awaitUser: bool = False
```

| 字段 | 含义 |
|---|---|
| `ok` | 工具是否成功 |
| `output` | 工具输出文本，会作为 `tool_result` 回给模型 |
| `backgroundTask` | 后台任务信息 |
| `awaitUser` | 是否暂停 turn 等用户 |

### 5.2 工具执行：`ToolRegistry.execute()`

`ToolRegistry.execute()` 的顺序：

```text
1. find(tool_name)
2. 找不到则返回 ToolResult(ok=False, output="Unknown tool")
3. parsed = tool.validator(input_data)
4. return tool.run(parsed, context)
5. 普通异常捕获成 ToolResult(ok=False, output="<type> error: <error>")
6. KeyboardInterrupt / SystemExit 不吞，继续向上抛
```

所以模型调用工具不是直接运行函数，而是经过：

```text
模型 tool_use
  ↓
agent_loop
  ↓
ToolRegistry.execute
  ↓
validator
  ↓
run
  ↓
ToolResult
```

### 5.3 默认注册工具

`create_default_tool_registry()` 注册：

```text
ask_user
remember

list_files
grep_files
read_file
write_file
modify_file
edit_file
patch_file

run_command
run_with_debug

web_fetch
web_search
api_tester

todo_write

git

notebook_edit

find_symbols
find_references
get_ast_info
multi_edit
code_review

file_tree
diff_viewer

test_runner

db_explorer
docker_helper

governance_audit

load_skill

MCP tools...
```

这些都会通过模型 API 的 `tools` 字段暴露给模型。

---

## 6. Skills 系统

相关文件：

```text
cortexterm/skills.py
cortexterm/tools/load_skill.py
```

工具注册前会先发现 skills：

```python
skills = [asdict(skill) for skill in discover_skills(cwd)]
```

### 6.1 Skill 搜索路径

`discover_skills(cwd)` 搜索：

```text
<cwd>/.cortexterm/skills/<name>/SKILL.md
~/.cortexterm/skills/<name>/SKILL.md
<cwd>/.claude/skills/<name>/SKILL.md
~/.claude/skills/<name>/SKILL.md
```

每个 skill 解析成：

```python
SkillSummary(
    name=...,
    description=...,
    path=...,
    source=...
)
```

### 6.2 Skill 如何进入 agent

skills 会进入两个地方：

第一，存在 `ToolRegistry` 里：

```python
ToolRegistry(..., skills=skills)
```

第二，进入 system prompt：

```python
build_system_prompt(..., extras={"skills": tools.get_skills()})
```

模型可以看到 skill 列表和说明，但具体读取某个 skill 的完整内容，需要调用 `load_skill` 工具。

---

## 7. MCP 系统

相关文件：

```text
cortexterm/mcp.py
```

工具注册时加载 MCP：

```python
mcp = create_mcp_backed_tools(
    cwd=cwd,
    mcp_servers=dict(runtime.get("mcpServers", {})) if runtime else {},
)
```

### 7.1 MCP 配置来源

MCP server 配置来自 runtime，也就是：

```text
~/.cortexterm/mcp.json
<cwd>/.mcp.json
~/.cortexterm/settings.json
~/.claude/settings.json
```

### 7.2 MCP server 启动流程

每个 server 构造成：

```python
StdioMcpClient(server_name, config, cwd)
```

然后：

```python
client.start()
```

启动顺序：

```text
1. 校验 command 是否安全
2. 校验 args 不含危险 shell 字符
3. subprocess.Popen 启动 MCP server
4. 尝试 content-length 或 newline-json 协议
5. 发送 initialize
6. 发送 notifications/initialized
7. 调 tools/list
8. 调 resources/list
9. 调 prompts/list
```

### 7.3 MCP 工具包装

MCP tool 会包装成 CortexTerm 工具名：

```text
mcp__<server>__<tool>
```

例如：

```text
mcp__github__create_issue
```

包装后的工具仍然是：

```python
ToolDefinition(
    name=wrapped_name,
    description=...,
    input_schema=...,
    validator=lambda value: value,
    run=lambda input_data: client.call_tool(...)
)
```

所以 agent loop 不区分“内置工具”和“MCP 工具”，它只认 `ToolDefinition`。

### 7.4 MCP resources 和 prompts

如果 MCP server 暴露 resources，额外注册：

```text
list_mcp_resources
read_mcp_resource
```

如果 MCP server 暴露 prompts，额外注册：

```text
list_mcp_prompts
get_mcp_prompt
```

### 7.5 MCP 关闭

`create_mcp_backed_tools()` 返回：

```python
{
    "tools": tools,
    "servers": servers,
    "dispose": lambda: [client.close() for client in clients],
}
```

`main.py` 最后会：

```python
tools.dispose()
```

这会关闭 MCP 子进程。

Windows 下 `close()` 会优先用：

```text
taskkill /T /F /PID
```

Unix 下会：

```text
terminate
超时后 kill
```

---

## 8. 权限系统

相关文件：

```text
cortexterm/permissions.py
cortexterm/workspace.py
```

初始化：

```python
prompt_handler = _make_cli_permission_prompt() if sys.stdin.isatty() else None
permissions = PermissionManager(cwd, prompt=prompt_handler)
```

### 8.1 权限持久化路径

权限存储在：

```text
~/.cortexterm/permissions.json
```

启动时会读取：

```text
allowedDirectoryPrefixes
deniedDirectoryPrefixes
allowedCommandPatterns
deniedCommandPatterns
allowedEditPatterns
deniedEditPatterns
```

运行时还有 session/turn 级权限：

```text
session_allowed_paths
session_denied_paths
session_allowed_commands
session_denied_commands
session_allowed_edits
session_denied_edits
turn_allowed_edits
turn_allow_all_edits
```

### 8.2 路径权限

工具处理路径通常先走：

```python
resolve_tool_path(context, input_path, intent)
```

它做：

```text
1. 如果 input_path 是绝对路径，直接用
2. 否则拼到 context.cwd
3. resolve()
4. 调 permissions.ensure_path_access()
5. 返回 Path
```

`ensure_path_access()` 策略：

```text
目标在 workspace_root 内:
    允许

命中 session/persistent denied:
    拒绝

命中 session/persistent allowed:
    允许

没有 prompt handler:
    报错，提示需要 TTY 审批

否则弹审批:
    y = allow once
    a = allow this directory
    n = deny once
    d = deny this directory
```

### 8.3 命令权限

`run_command` 调用：

```python
permissions.ensure_command(command, args, command_cwd, force_prompt_reason)
```

命令判断有两层。

第一层在 `run_command.py`：

```text
readonly 命令：
  pwd, ls, find, rg, grep, cat, head, tail, wc, sed, echo, df, du, whoami,
  dir, type, where, findstr, more, hostname

development 命令：
  git, npm, node, python, python3, pytest, bash, sh,
  pip, cargo, go, make, cmake, dotnet, powershell, pwsh, cmd

shell snippet：
  command 中含 | & ; < > ( ) $ `

unknown command：
  触发 forced approval
```

第二层在 `permissions.py`：

```text
git reset --hard
git clean
git checkout --
git push --force
npm publish
rm -rf
dd / mkfs / fdisk / format
chmod 777
node/python/bash/powershell 等 arbitrary local code
diskutil / csrutil / defaults write / launchctl unload / dscl
```

如果需要审批：

```text
y = allow once
a = always allow this command
n = deny once
d = always deny this command
```

### 8.4 编辑权限

写文件、改文件、patch 文件最终会调用：

```python
permissions.ensure_edit(target_path, diff_preview)
```

审批选项：

```text
1 apply once
2 allow this file in this turn
3 allow all edits in this turn
4 always allow this file
5 reject once
6 reject and send guidance to model
7 always reject this file
```

如果用户选 `deny_with_feedback`，错误里会带：

```text
User guidance: ...
```

这个错误会作为工具失败结果反馈给模型。

---

## 9. 模型适配器选择

`main.py` 里选择模型：

```python
model = (
    MockModelAdapter()
    if runtime is None or os.environ.get("CORTEXTERM_MODEL_MODE") == "mock"
    else AnthropicModelAdapter(runtime, tools)
)
```

也就是：

| 条件 | 模型 |
|---|---|
| `runtime is None` | `MockModelAdapter` |
| `CORTEXTERM_MODEL_MODE=mock` | `MockModelAdapter` |
| 正常配置 | `AnthropicModelAdapter` |

### 9.1 MockModelAdapter

文件：

```text
cortexterm/mock_model.py
```

它是硬编码 fallback，不是真模型。

它识别：

```text
/tools
/ls
/grep
/read
/cmd
/write
/edit
/patch
```

并返回：

```python
AgentStep(type="tool_calls", calls=[...])
```

或者直接返回 assistant 文本。

### 9.2 AnthropicModelAdapter

文件：

```text
cortexterm/anthropic_adapter.py
```

真实模型调用在：

```python
AnthropicModelAdapter.next(messages)
```

它做：

```text
1. 把 CortexTerm 内部 messages 转成 Anthropic messages
2. 构造 request_body
3. POST 到 runtime["baseUrl"]/v1/messages
4. 解析响应 content blocks
5. 返回 AgentStep
```

---

## 10. System Prompt 构造

文件：

```text
cortexterm/prompt.py
```

`main.py` 创建初始 messages：

```python
messages = [
    {
        "role": "system",
        "content": build_system_prompt(
            cwd,
            permissions.get_summary(),
            {
                "skills": tools.get_skills(),
                "mcpServers": tools.get_mcp_servers(),
                "memory_context": memory_mgr.get_relevant_context(),
            },
        ),
    }
]
```

每次用户输入自然语言前，也会刷新 `messages[0]`。

### 10.1 system prompt 包含什么

`build_system_prompt()` 拼接：

```text
身份:
  You are cortexterm, a terminal coding assistant.

默认行为:
  inspect repository
  use tools
  make code changes when appropriate
  explain results clearly

当前 cwd

权限上下文

工具使用约束:
  需要澄清时调用 ask_user
  read_file TRUNCATED: yes 时继续 offset 读
  用户命名 skill 时调用 load_skill

结构化响应协议:
  <progress>
  <final>
  ask_user
  普通 assistant text 视为本 turn 完成

Memory policy:
  什么时候 remember
  不能记 secrets / 临时输出 / 猜测 / 大日志

Engineering Governance Rules

Available skills

Configured MCP servers

Memory context
```

### 10.2 最关键协议

system prompt 要求模型：

```text
还在工作时，用 <progress>
完成时，用 <final>
需要澄清时调用 ask_user
progress 后不要停止
普通 assistant text 被当作本 turn 完成
```

但注意：prompt 只是给模型的规则；真正解释 `<progress>`、`<final>` 的是 `anthropic_adapter.py` 和 `agent_loop.py`。

---

## 11. ContextManager

文件：

```text
cortexterm/context_manager.py
```

如果 runtime 存在：

```python
context_mgr = ContextManager(model=runtime.get("model", "default"))
```

### 11.1 token 估算

默认上下文窗口：

```text
claude-sonnet-4-20250514  200,000
claude-opus-4-20250514    200,000
claude-haiku-3-20240307   100,000
gpt-4o                    128,000
gpt-4o-mini               128,000
gpt-4-turbo               128,000
default                   128,000
```

估算规则：

```text
英文/代码：约 4 字符/token
CJK：约 1.5 字符/token
```

### 11.2 agent turn 开始时检查上下文

`run_agent_turn()` 一开始：

```python
context_manager.messages = current_messages
stats = context_manager.get_stats()
if context_manager.should_auto_compact():
    current_messages = context_manager.compact_messages()
```

压缩阈值：

```python
AUTOCOMPACT_THRESHOLD = 0.95
```

压缩策略：

```text
1. 保留 system prompt
2. 删除旧 assistant_progress
3. 优先删除旧 tool_call/tool_result pair
4. 再删除旧 user/assistant
5. 保留最近 MIN_MESSAGES_TO_KEEP 条
6. 插入 compaction marker
```

---

## 12. MemoryManager

文件：

```text
cortexterm/memory.py
cortexterm/tools/remember.py
```

初始化：

```python
memory_mgr = MemoryManager(project_root=Path(cwd))
```

### 12.1 三层记忆

```text
user     ~/.cortexterm/memory/
project  <workspace>/.cortexterm-memory/
local    <workspace>/.cortexterm-memory-local/
```

每层可能有：

```text
MEMORY.md
memory.json
```

### 12.2 注入 prompt

每轮刷新 system prompt 时：

```python
"memory_context": memory_mgr.get_relevant_context()
```

所以长期记忆生效链路是：

```text
启动加载 memory
  ↓
每轮 build_system_prompt 注入 memory_context
  ↓
模型看到历史事实
```

### 12.3 remember 工具

模型可以调用：

```text
remember
```

流程：

```text
1. 校验 scope/category/content/tags
2. 检测敏感内容：
   sk-...
   api_key/token/password/secret
   bearer ...
   AKIA...
3. 检查重复，similarity >= 0.88 则跳过
4. 调 permissions.ensure_edit() 审批 memory 写入
5. manager.add_entry()
6. 返回 Memory saved / Memory skipped
```

---

## 13. 当前 TUI 路由

`main.py` 最终会在三条入口里选一条：

```text
非交互 stdin        -> 逐行读取 stdin，适合管道/脚本
默认交互式终端      -> tty_app.py::run_tty_app()
```

当前用户正常在终端里运行：

```powershell
python -m cortexterm.main
```

会进入当前 TUI：

```python
run_tty_app(...)
```

相关文件：

```text
cortexterm/tty_app.py
cortexterm/tui/chrome.py
cortexterm/tui/input.py
cortexterm/tui/input_parser.py
cortexterm/tui/screen.py
cortexterm/tui/transcript.py
cortexterm/tui/types.py
```

本文只讲当前默认 TUI。下面说的 TUI 都指 `tty_app.py::run_tty_app()` 这一条链路。

当前 TUI 的核心特征：

```text
1. 进入 alternate screen
2. 切 raw mode 接管键盘/鼠标输入
3. 首页显示 MiniClaudeCode 启动页和输入卡片
4. 用户提交后切到 conversation 页面
5. conversation 区显示 user / assistant / progress / tool entry
6. 底部 input 使用 user >，支持多行输入
7. 工具结果默认折叠成 tool card，可点击标题行展开/收起
8. 权限审批用 pending approval 页面处理
9. agent turn 在后台线程执行
10. 主线程持续处理输入、滚动、鼠标点击、重绘、autosave
```

核心仍然不是 UI，而是：

```python
run_agent_turn(...)
```

TUI 只是负责把用户输入、工具状态、权限等待、最终回答用更可读的方式展示出来。

---

## 14. 当前 TUI 的自然语言输入流程

用户在首页或 conversation 底部输入：

```text
user > 帮我修这个 bug
```

按回车后，事件链路是：

```text
1. raw stdin 读到按键
2. parse_input_chunk() 解析成 KeyEvent/TextEvent/MouseEvent/WheelEvent
3. _handle_event()
4. _handle_normal_mode_event()
5. _handle_normal_mode_key()
6. _handle_normal_mode_return()
```

`_handle_normal_mode_return()` 做几件关键事：

```text
1. 取出 state.input 作为 submitted
2. 清空输入框和 cursor
3. 如果 submitted 非空，关闭首页 show_welcome
4. 调 _handle_input(args, state, rerender, submitted)
5. rerender()
```

`_handle_input()` 先处理本地命令：

```text
/exit
/transcript-save
/debug-scroll
本地 slash command
本地 tool shortcut
未知 slash command
```

如果不是本地命令，就作为自然语言进入 agent turn。它会先写 transcript：

```python
_push_transcript_entry(state, kind="user", body=input_text)
```

然后刷新 system prompt：

```python
messages[0] = {
    "role": "system",
    "content": build_system_prompt(
        cwd,
        permissions.get_summary(),
        {
            "skills": args.tools.get_skills(),
            "mcpServers": args.tools.get_mcp_servers(),
            "memory_context": args.memory_mgr.get_relevant_context() if args.memory_mgr else "",
        },
    ),
}
```

再加入用户消息：

```python
messages.append({"role": "user", "content": input_text})
```

然后设置状态并启动后台线程：

```text
state.status = "thinking"
state.agent_busy = True
_run_agent_background(...)
```

后台线程里才真正调用：

```python
run_agent_turn(...)
```

主线程不阻塞，它继续处理：

```text
滚轮滚动 conversation
点击 tool card 标题行展开/折叠
Ctrl+R 进入 reading mode
权限审批选择
周期性 rerender
session autosave
```

---

## 15. agent 核心循环：`run_agent_turn()`

文件：

```text
cortexterm/agent_loop.py
```

函数签名：

```python
def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    max_steps: int = 50,
    on_tool_start=None,
    on_tool_result=None,
    on_assistant_message=None,
    on_progress_message=None,
    context_manager=None,
) -> list[ChatMessage]:
```

### 15.1 初始化状态

```python
current_messages = list(messages)
saw_tool_result = False
empty_response_retry_count = 0
recoverable_thinking_retry_count = 0
tool_error_count = 0
step = 0
```

含义：

| 变量 | 作用 |
|---|---|
| `current_messages` | 当前 turn 的工作消息副本 |
| `saw_tool_result` | 是否已经执行过工具 |
| `empty_response_retry_count` | 空响应重试次数 |
| `recoverable_thinking_retry_count` | thinking pause/max_tokens 恢复次数 |
| `tool_error_count` | 工具失败次数 |
| `step` | 当前模型调用轮次 |

### 15.2 context compaction

如果有 `context_manager`：

```python
context_manager.messages = current_messages
stats = context_manager.get_stats()
if context_manager.should_auto_compact():
    current_messages = context_manager.compact_messages()
    if on_assistant_message:
        on_assistant_message(context_manager.get_context_summary())
```

### 15.3 主循环

```python
while max_steps is None or step < max_steps:
    step += 1
    next_step = model.next(current_messages)
```

默认最多 50 步。

---

## 16. `model.next()`：模型协议层

真实模型：

```python
AnthropicModelAdapter.next(messages)
```

文件：

```text
cortexterm/anthropic_adapter.py
```

### 16.1 内部消息格式

CortexTerm 内部消息角色：

```text
system
user
assistant
assistant_progress
assistant_tool_call
tool_result
```

定义在：

```text
cortexterm/types.py
```

### 16.2 转换成 Anthropic messages

`_to_anthropic_messages(messages)` 做：

```text
system:
  拼成单独 system string

user:
  user text block

assistant:
  assistant text block

assistant_progress:
  assistant text block:
  <progress>
  ...
  </progress>

assistant_tool_call:
  assistant tool_use block

tool_result:
  user tool_result block
```

### 16.3 请求体

```python
request_body = {
    "model": self.runtime["model"],
    "system": system_message,
    "messages": converted_messages,
    "tools": [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in self.tools.list()
    ],
}
```

如果有 `maxOutputTokens`：

```python
request_body["max_tokens"] = self.runtime["maxOutputTokens"]
```

请求地址：

```text
<baseUrl>/v1/messages
```

请求头：

```text
content-type: application/json
anthropic-version: 2023-06-01
x-api-key: ...
或 Authorization: Bearer ...
```

### 16.4 retry 策略

adapter 内置 retry：

```text
默认 max retries = 4
429 或 5xx 重试
指数退避
base 500ms
max 8000ms
支持 Retry-After
支持 CORTEXTERM_MAX_RETRIES
支持 CORTEXTERM_REQUEST_TIMEOUT_SECONDS
```

### 16.5 响应解析

响应 `content` block：

```text
text               -> text_parts
tool_use           -> tool_calls
thinking           -> thinking_blocks
redacted_thinking  -> thinking_blocks
其他 block          -> ignored_block_types
```

文本会识别：

```text
<final>...</final>       -> kind="final"
[FINAL]                  -> kind="final"
<progress>...</progress> -> kind="progress"
[PROGRESS]               -> kind="progress"
普通文本                  -> kind=None
```

如果有工具调用，返回：

```python
AgentStep(
    type="tool_calls",
    calls=tool_calls,
    thinkingBlocks=thinking_blocks,
    content=parsed_text,
    contentKind="progress" if kind == "progress" else None,
    diagnostics=diagnostics,
)
```

如果没有工具调用，返回：

```python
AgentStep(
    type="assistant",
    content=parsed_text,
    thinkingBlocks=thinking_blocks,
    kind=kind,
    diagnostics=diagnostics,
)
```

---

## 17. agent_loop 如何处理 assistant 文本

如果：

```python
next_step.type == "assistant"
```

进入 assistant 分支。

### 17.1 空响应

```python
is_empty = len(next_step.content.strip()) == 0
```

### 17.2 progress

如果 `kind == "progress"`：

```text
1. on_progress_message(content)
2. append {"role": "assistant_progress", "content": content}
3. append user nudge，要求继续
4. continue while
```

nudge 会要求模型继续具体行动，不要停在进度描述。

### 17.3 thinking pause / max_tokens

如果：

```text
content 为空
stop_reason 是 pause_turn 或 max_tokens
ignored_block_types 包含 thinking
```

则认为模型在 thinking 阶段停住。

最多恢复 3 次。

恢复方式：

```text
1. on_progress_message("Model hit max_tokens..." 或 "Model returned pause_turn...")
2. append assistant_progress
3. append user nudge:
   RESUME_AFTER_PAUSE
   或 RESUME_AFTER_MAX_TOKENS
4. continue
```

### 17.4 普通空响应重试

如果空响应但不是 thinking stop：

```text
最多重试 2 次
```

追加 nudge：

```text
如果已经执行过工具：
  NUDGE_AFTER_EMPTY_RESPONSE
否则：
  NUDGE_AFTER_EMPTY_NO_TOOLS
```

### 17.5 空响应最终失败

如果重试用完：

```text
Model returned an empty response...
```

append assistant fallback，然后 return。

### 17.6 正常 assistant 结束

如果是普通最终输出：

```python
on_assistant_message(next_step.content)
current_messages.append({"role": "assistant", "content": next_step.content})
return current_messages
```

这就是一次 turn 正常结束。

---

## 18. agent_loop 如何处理工具调用

如果：

```python
next_step.type == "tool_calls"
```

进入工具分支。

### 18.1 tool call 同时带文本

如果 `next_step.content` 非空：

```text
如果 contentKind == progress:
  on_progress_message
  append assistant_progress
  append NUDGE_CONTINUE

否则:
  on_assistant_message
  append assistant
```

然后继续处理工具。

### 18.2 记录 assistant_tool_call

对每个 call：

```python
current_messages.append(
    {
        "role": "assistant_tool_call",
        "toolUseId": call["id"],
        "toolName": call["toolName"],
        "input": call["input"],
        "thinkingBlocks": next_step.thinkingBlocks,  # 第一个 call 才带
    }
)
```

这一步很关键：它保存了“模型决定调用工具”的事实。下一次请求模型时，adapter 会把它还原成 Anthropic 的 `tool_use` block。

### 18.3 顺序执行工具

然后逐个执行：

```python
for call in next_step.calls:
    on_tool_start(call["toolName"], call["input"])

    result = tools.execute(
        call["toolName"],
        call["input"],
        ToolContext(cwd=cwd, permissions=permissions),
    )

    on_tool_result(call["toolName"], result.output, not result.ok)
```

注意：这里是顺序执行，不是并发执行。

### 18.4 记录 tool_result

工具结果 append：

```python
current_messages.append(
    {
        "role": "tool_result",
        "toolUseId": call["id"],
        "toolName": call["toolName"],
        "content": result.output,
        "isError": not result.ok,
    }
)
```

如果工具失败：

```python
tool_error_count += 1
```

但不会直接停止。工具失败会回给模型，让模型下一轮自己修正。

### 18.5 awaitUser

如果：

```python
result.awaitUser
```

则：

```python
on_assistant_message(result.output)
current_messages.append({"role": "assistant", "content": result.output})
return current_messages
```

典型工具：

```text
ask_user
```

这就是模型需要澄清时等待用户的机制。

### 18.6 工具后继续模型

如果没有 awaitUser，则回到 while：

```text
工具结果已经 append 到 messages
  ↓
下一轮 model.next(current_messages)
  ↓
模型看到 tool_result
  ↓
继续调用工具或给最终回答
```

---

## 19. max_steps 兜底

如果循环超过 50 步：

```python
fallback = "Reached the maximum tool step limit for this turn."
```

然后：

```python
on_assistant_message(fallback)
append assistant fallback
return current_messages
```

---

## 20. 错误处理

### 20.1 模型错误

`run_agent_turn()` 捕获：

```text
ConnectionError
TimeoutError
Exception
```

转成 assistant 文本：

```text
Network error...
Model API timeout...
Model API error (<type>): ...
```

然后 append assistant 并 return。

`KeyboardInterrupt` 不吞，继续向上抛。

### 20.2 工具错误

`ToolRegistry.execute()` 捕获普通异常：

```python
return ToolResult(ok=False, output=f"{type(tool).__name__} error: {error}")
```

所以工具异常通常变成 tool_result，不会炸掉整个 agent。

`KeyboardInterrupt` 和 `SystemExit` 不吞。

### 20.3 权限拒绝

权限拒绝一般是 `PermissionManager` 抛 `RuntimeError`。

因为它发生在工具 `run()` 内部，通常会被 `ToolRegistry.execute()` 包成失败 ToolResult，再作为 `tool_result` 反馈给模型。

---

## 21. 具体工具模块

### 21.1 `read_file`

文件：

```text
cortexterm/tools/read_file.py
```

输入：

```json
{
  "path": "cortexterm/main.py",
  "offset": 0,
  "limit": 8000
}
```

默认：

```text
DEFAULT_READ_LIMIT = 8000
MAX_READ_LIMIT = 20000
```

流程：

```text
1. resolve_tool_path(context, path, "read")
2. 读取 UTF-8 文本
3. 使用 2 秒 TTL 文件缓存
4. 根据 offset/limit 截取内容
5. 返回 header + chunk
```

输出 header：

```text
FILE: ...
OFFSET: ...
END: ...
TOTAL_CHARS: ...
TRUNCATED: yes - call read_file again with offset ...
```

### 21.2 `write_file`

文件：

```text
cortexterm/tools/write_file.py
```

流程：

```text
1. validate path/content
2. resolve_tool_path(..., "write")
3. apply_reviewed_file_change()
4. 生成 diff
5. permissions.ensure_edit()
6. 审批通过后写入
```

### 21.3 `edit_file`

文件：

```text
cortexterm/tools/edit_file.py
```

流程：

```text
1. validate path/search/replace/replace_all
2. normalize CRLF 到 LF
3. 读取原文件
4. 检查 search 是否存在
5. 替换一次或全部替换
6. apply_reviewed_file_change()
```

### 21.4 `patch_file`

文件：

```text
cortexterm/tools/patch_file.py
```

流程：

```text
1. validate path/replacements
2. 读取原文件
3. 按顺序应用多个 exact-text replacement
4. 如果某个 search 找不到，返回 ToolResult(ok=False)
5. apply_reviewed_file_change()
6. 返回 patch summary
```

### 21.5 `run_command`

文件：

```text
cortexterm/tools/run_command.py
```

输入：

```json
{
  "command": "pytest -q",
  "args": [],
  "cwd": null
}
```

流程：

```text
1. 如果 cwd 存在，resolve_tool_path(..., "list")
2. split_command_line()
3. 判断是否 shell snippet
4. 判断是否 background shell snippet
5. 判断 known command / readonly command
6. 构造实际执行命令
7. permissions.ensure_command()
8. 如果是后台命令，subprocess.Popen 并注册 background task
9. 否则 subprocess.run()
10. capture stdout/stderr
11. returncode == 0 则 ok=True
```

超时：

```text
COMMAND_TIMEOUT = 300 秒
```

后台命令条件：

```text
shell snippet
且以 & 结尾
且不是 &&
```

### 21.6 `web_search`

文件：

```text
cortexterm/tools/web_search.py
```

流程：

```text
1. validate query / num_results
2. 请求 DuckDuckGo HTML:
   https://html.duckduckgo.com/html/?q=...
3. urllib 读取 HTML
4. regex 解析 result__a + result__snippet
5. 返回 title/url/snippet
```

限制：

```text
默认 5 条
最多 10 条
snippet 截断到 200 字符
```

注意：这里的摘要是 DuckDuckGo 搜索结果页 snippet，不是网页正文前 200 字符，也不是 CortexTerm 自己用模型生成的摘要。

### 21.7 `web_fetch`

文件：

```text
cortexterm/tools/web_fetch.py
```

流程：

```text
1. validate URL
2. SSRF 安全检查
3. 阻止 localhost / 127 / 10 / 192.168 / 172.16 / 0.0.0.0 / ::1 / fe80
4. urllib 请求
5. redirect 最多 5 次
6. 根据 charset 解码
7. 如果是 HTML，去 script/style/tag 并抽取纯文本
8. max_chars 截断
9. 返回 header + text
```

### 21.8 `ask_user`

文件：

```text
cortexterm/tools/ask_user.py
```

返回：

```python
ToolResult(ok=True, output=question, awaitUser=True)
```

所以 agent_loop 会结束当前 turn，等待用户输入。

### 21.9 `todo_write`

文件：

```text
cortexterm/tools/todo_write.py
```

维护进程内 todo list：

```text
pending
in_progress
completed
```

每次调用传完整列表，它会清空旧 tasks 并替换。

### 21.10 `remember`

文件：

```text
cortexterm/tools/remember.py
```

保存长期记忆，带敏感内容检测、重复检测、权限审批。

### 21.11 其他工具

| 工具 | 文件 | 作用 |
|---|---|---|
| `list_files` | `tools/list_files.py` | 列目录 |
| `grep_files` | `tools/grep_files.py` | 搜索文本 |
| `modify_file` | `tools/modify_file.py` | 整文件替换 |
| `multi_edit` | `tools/multi_edit.py` | 多编辑 |
| `notebook_edit` | `tools/notebook_edit.py` | Notebook JSON 编辑 |
| `git` | `tools/git.py` | status/diff/log/commit/review |
| `run_with_debug` | `tools/run_with_debug.py` | 运行命令并解析错误 |
| `test_runner` | `tools/test_runner.py` | 测试发现、运行、解析 |
| `code_review` | `tools/code_review.py` | AST 静态检查 |
| `code_nav` | `tools/code_nav.py` | symbols/references/AST info |
| `file_tree` | `tools/file_tree.py` | 目录树 |
| `diff_viewer` | `tools/diff_viewer.py` | diff 展示 |
| `db_explorer` | `tools/db_explorer.py` | 数据库 schema/query |
| `docker_helper` | `tools/docker_helper.py` | Docker/Compose 辅助 |
| `api_tester` | `tools/api_tester.py` | HTTP API 测试 |
| `governance_audit` | `tools/governance_audit_tool.py` | 工程治理审计 |

---

## 22. Slash commands 和快捷工具命令

### 22.1 `cli_commands.py`

文件：

```text
cortexterm/cli_commands.py
```

定义 slash command 列表：

```text
/help
/tools
/status
/cost
/context
/tasks
/memory
/config
/history
/clear
/retry
/transcript-save
/model
/config-paths
/skills
/mcp
/permissions
/exit
/debug
/ls
/grep
/read
/write
/modify
/edit
/patch
/cmd
```

`try_handle_local_command()` 当前直接处理部分命令：

```text
/help
/config-paths
/permissions
/skills
/config
/memory
```

注意：列表里有些命令声明了，但当前 TUI 不一定都把高级交互做成独立页面；未特殊处理的命令会走通用本地命令或工具快捷路径。

### 22.2 `local_tool_shortcuts.py`

文件：

```text
cortexterm/local_tool_shortcuts.py
```

把 slash 输入直接转工具：

```text
/ls      -> list_files
/grep    -> grep_files
/read    -> read_file
/write   -> write_file
/modify  -> modify_file
/edit    -> edit_file
/patch   -> patch_file
/cmd     -> run_command
```

这条路径绕过模型。

例如：

```text
/read cortexterm/main.py
```

会直接执行：

```text
tools.execute("read_file", {"path": "cortexterm/main.py"})
```

---

## 23. 当前 TUI 细节

文件：

```text
cortexterm/tty_app.py
cortexterm/tui/chrome.py
cortexterm/tui/input_parser.py
cortexterm/tui/screen.py
cortexterm/tui/transcript.py
cortexterm/tui/types.py
```

### 23.1 session 初始化

`run_tty_app()` 会处理：

```text
--list-sessions
--resume latest
--resume <id>
没有 resume 时创建新 session
```

### 23.2 raw mode 和 alternate screen

当前 TUI 会：

```python
enter_alternate_screen()
hide_cursor()
with _RawModeContext():
    ...
```

退出时：

```python
show_cursor()
exit_alternate_screen()
```

这意味着界面不是普通终端一行行 append，而是 TUI 自己重绘当前屏幕。

### 23.3 首页和 conversation 页面

启动后，如果没有进入 pending approval / reading mode，且 `state.show_welcome` 为 True，会渲染首页：

```python
_render_home_screen(args, state)
```

首页包含：

```text
MiniClaudeCode logo
输入卡片
模型 / ready 状态
快捷提示
```

用户第一次提交非空输入后：

```python
state.show_welcome = False
```

之后进入 conversation 页面。conversation 页面主要由：

```python
render_transcript(...)
_render_session_prompt(state)
_render_footer_cached(...)
```

组成。

### 23.4 input 输入区

底部输入区由：

```text
cortexterm/tui/input.py::render_input_prompt()
```

渲染。

当前 prompt 语义是：

```text
user >
```

输入框支持多行和长文本换行。首页输入卡片由 `tty_app.py::_render_home_input_card()` 渲染，也按终端显示宽度换行。

### 23.5 conversation / transcript

conversation 区显示 `TranscriptEntry`：

```python
TranscriptEntry(
    id,
    kind,
    toolName,
    status,
    body,
    collapsed,
    collapsedSummary,
    collapsePhase,
    revealLines,
    transition,
)
```

常见 `kind`：

```text
user
assistant
progress
tool
system
```

渲染入口：

```python
cortexterm/tui/transcript.py::render_transcript()
```

如果 viewport 从某条消息中间开始显示，渲染层会补上该消息的角色标签，避免顶部出现没有称谓的裸文本。

### 23.6 工具卡片

工具开始时，TUI 创建 running tool entry：

```python
_push_transcript_entry(kind="tool", status="running", ...)
```

工具结束时，更新为 success/error：

```python
_update_tool_entry(...)
_apply_tool_result_visual_state(...)
```

成功工具默认折叠成摘要：

```text
▸ tool read_file ok [click to expand]
  FILE: ...
```

展开时：

```text
▾ tool read_file ok [click to collapse]
  ...
```

鼠标点击通过 `state.mouse_zones` 命中工具卡标题行，然后调用：

```python
_toggle_tool_entry(...)
```

展开/收起动画通过：

```python
_animate_tool_open(...)
_animate_tool_close(...)
```

控制 `revealLines` 和 `transition`。

### 23.7 reading mode

`Ctrl+R` 会切换：

```python
state.transcript_read_mode
```

reading mode 会把大部分屏幕交给 conversation，用来读旧消息。键盘/鼠标滚动由：

```python
_handle_transcript_read_mode_event(...)
```

处理。

### 23.8 主事件循环

主线程做：

```text
1. autosave save_if_needed()
2. 检查后台 agent thread 是否完成
3. 读取键盘/鼠标输入
4. parse_input_chunk()
5. _handle_event()
6. throttled.flush()
```

Windows 使用 `msvcrt`，Unix 使用 `select + os.read`。

### 23.9 后台 agent thread

当前 TUI 不在主线程同步跑 agent，而是：

```python
agent_thread = threading.Thread(target=_run_agent_background, daemon=True)
agent_thread.start()
```

后台线程里调用：

```python
run_agent_turn(...)
```

主线程继续渲染。

### 23.10 权限审批

当前 TUI 把：

```python
permissions.prompt = _permission_prompt_handler
```

这个 handler：

```text
1. 设置 state.pending_approval
2. rerender()
3. approval_event.wait()
4. 用户在 UI 里选择
5. approval_event.set()
6. 返回 approval_result
```

这样避免后台线程和主线程同时写 terminal。

---

## 24. Session 持久化

文件：

```text
cortexterm/session.py
```

路径：

```text
~/.cortexterm/sessions/
~/.cortexterm/sessions_index.json
```

### 24.1 SessionData

保存：

```text
session_id
created_at
updated_at
workspace
messages
transcript_entries
history
permissions_summary
skills
mcp_servers
metadata
```

### 24.2 AutosaveManager

默认：

```text
AUTOSAVE_INTERVAL_SECONDS = 30
```

当前 TUI 主循环会定期：

```python
state.autosave.save_if_needed()
```

退出时强制：

```python
state.autosave.force_save()
```

非当前 TUI 的兼容终端模式主要维护 history 和本进程内 transcript/tool cache；当前 TUI 才是完整 session autosave 的主路径。

---

## 25. History 和 Transcript

### 25.1 History

文件：

```text
cortexterm/history.py
```

路径：

```text
~/.cortexterm/history.json
```

当前 TUI、stdin 和兼容终端模式都会保存用户输入 history。

它不是完整 session，只是 prompt 历史。

### 25.2 Transcript 保存

命令：

```text
/transcript-save <path>
```

调用：

```python
_save_transcript_file(cwd, permissions, transcript, output_path)
```

流程：

```text
1. resolve_tool_path(..., "write")
2. 创建父目录
3. format_transcript_text(transcript)
4. 写入文件
```

格式化在：

```text
cortexterm/tui/transcript.py
```

---

## 26. 后台任务系统

文件：

```text
cortexterm/background_tasks.py
```

进程内维护：

```python
_background_tasks: dict[str, dict[str, Any]] = {}
```

只有 `run_command` 的后台 shell snippet 会注册：

```python
register_background_shell_task(command, pid, cwd)
```

状态刷新：

```text
Windows:
  OpenProcess + GetExitCodeProcess

Unix:
  os.kill(pid, 0)
```

当前 TUI 会导入 `list_background_tasks()` 用于显示。

---

## 27. 扩展/部分接入模块

这些模块存在，但不是每次 agent turn 必经主链路。

### 27.1 `state.py`

实现 Zustand-style store：

```text
Store
AppState
create_app_store
format_app_state_summary
```

当前 TUI 创建 app state store，并在 busy 状态时更新。

### 27.2 `cost_tracker.py`

定义：

```text
ModelUsage
CostTracker
```

可统计 tokens、cost、errors、code changes。

但当前真实 `AnthropicModelAdapter` 没有完整写入 usage/cost，所以它是部分接入。

### 27.3 `poly_commands.py`

设计了更完整的命令系统：

```text
PromptCommand
LocalCommand
InteractiveCommand
CommandRegistry
```

内置：

```text
/cost
/status
/context
/memory
/tasks
```

但当前 UI 主命令处理仍主要靠 `cli_commands.py` 和 `local_tool_shortcuts.py`。

### 27.4 `task_tracker.py`

定义完整任务系统：

```text
TaskStatus
Task
TaskList
TaskManager
```

但 agent loop 不会自动把模型计划转成 `TaskManager`。当前更多是备用/展示框架。

### 27.5 `hooks.py`

定义：

```text
HookEvent
HookContext
HookManager
create_logging_hook
create_script_hook
register_hook
fire_hook_sync
```

但主 `run_agent_turn()` 没有系统性触发 hook。

### 27.6 `auto_mode.py`

定义：

```text
PermissionMode
RiskLevel
RiskAssessment
AutoModeChecker
ModeState
```

但当前权限主链路走 `permissions.py`，不是 `auto_mode.py`。

### 27.7 `async_context.py`

定义异步上下文收集和缓存。

当前 `main.py` 的 system prompt 主链路没有直接调用它。

### 27.8 `api_retry.py`

实现通用 retry 框架。

当前 `anthropic_adapter.py` 自己实现 retry，没有使用这个通用模块。

### 27.9 `sub_agents.py`

定义子代理框架和 `should_use_sub_agent()`。

当前 `run_agent_turn()` 没有自动调 sub-agent。

---

## 28. 一次完整自然语言 turn 的精确时序

以当前 TUI 为例，用户输入：

```text
user > 帮我看一下为什么测试失败
```

完整时序：

```text
1. 当前 TUI 在 raw mode 中读到用户回车
2. parse_input_chunk() 解析按键
3. _handle_event()
4. _handle_normal_mode_event()
5. _handle_normal_mode_return()
6. 取出 state.input，清空输入框
7. 如果是首页，设置 state.show_welcome = False
8. _handle_input(args, state, rerender, submitted)
9. 写入 history
10. 检查 /exit、/transcript-save、/debug-scroll 等本地命令
11. 检查 try_handle_local_command()
12. 检查 parse_local_tool_shortcut()
13. 如果未知 slash command，向 transcript 写 system 提示
14. 否则作为自然语言输入
15. _push_transcript_entry(kind="user", body=input_text)
16. 刷新 messages[0] system prompt
17. messages.append({"role": "user", "content": input_text})
18. state.status = "thinking"
19. state.agent_busy = True
20. 启动后台 agent thread
21. 后台线程设置权限 turn 状态
22. 后台线程调 run_agent_turn()
23. run_agent_turn 复制 messages 到 current_messages
24. 如有 context_mgr，计算 context stats
25. 如需 compact，压缩 messages
26. step = 1
27. 调 model.next(current_messages)
28. AnthropicModelAdapter:
      转换 messages
      构造 tools schema
      POST /v1/messages
      解析 text/tool_use/thinking
      返回 AgentStep
29. 如果 AgentStep 是 assistant:
      处理 progress/final/empty/pause/max_tokens
      如果最终 assistant，则 callback 展示，append assistant，return
30. 如果 AgentStep 是 tool_calls:
      如果有 progress 文本，先展示 progress
      append assistant_tool_call
      对每个工具:
        on_tool_start
        tools.execute()
          find tool
          validator
          run
          permissions / filesystem / subprocess / network / MCP
        on_tool_result
        append tool_result
        如果 awaitUser，append assistant 并 return
      回到 while
31. 下一轮 model.next(current_messages)
32. 模型看到 tool_result 后继续:
      可能继续 tool_calls
      可能给最终 assistant
33. 最终 assistant 出现:
      on_assistant_message
      append assistant
      return messages
34. 后台线程 finally:
      permissions.end_turn()
      state.agent_busy = False
      state.status = None
35. 主线程继续渲染 conversation 和底部 user > 输入框
```

---

## 29. 工具调用在 messages 里的形态

假设模型调用：

```json
{
  "id": "toolu_123",
  "name": "read_file",
  "input": {
    "path": "cortexterm/main.py"
  }
}
```

CortexTerm 先 append：

```python
{
    "role": "assistant_tool_call",
    "toolUseId": "toolu_123",
    "toolName": "read_file",
    "input": {"path": "cortexterm/main.py"},
}
```

工具执行后 append：

```python
{
    "role": "tool_result",
    "toolUseId": "toolu_123",
    "toolName": "read_file",
    "content": "FILE: cortexterm/main.py\nOFFSET: ...",
    "isError": False,
}
```

下一轮请求模型时，adapter 把它转成：

```text
assistant tool_use block
user tool_result block
```

这就是模型能看到工具结果的原因。

---

## 30. 三种停止方式

### 30.1 正常最终回答

模型返回：

```text
<final>...</final>
```

或者普通 assistant 文本。

agent_loop append assistant，然后 return。

### 30.2 ask_user 暂停

模型调用：

```text
ask_user
```

工具返回：

```python
awaitUser=True
```

agent_loop append assistant question，然后 return。

用户下一轮回答后继续。

### 30.3 异常/兜底停止

包括：

```text
模型 API 错误
模型空响应重试用完
max_steps 达到 50
KeyboardInterrupt
```

前三个生成 assistant fallback。  
KeyboardInterrupt 向上抛，`main.py` 捕获后优雅关闭。

---

## 31. 关闭流程

`main.py` 外层：

```python
try:
    ...
except KeyboardInterrupt:
    print("Interrupted by user...")
finally:
    tools.dispose()
```

关闭流程：

```text
1. 当前 TUI 先保存 session
2. show_cursor()
3. exit_alternate_screen()
4. tools.dispose()
5. MCP clients close()
6. 日志记录 shutdown complete
```

---

## 32. 当前代码里需要特别注意的事实

### 32.1 Agent 策略 = prompt + loop，不是单独一个地方

`prompt.py` 告诉模型应该怎么做：

```text
用工具
用 <progress>/<final>
需要澄清用 ask_user
```

但真正强制调度的是：

```text
agent_loop.py
```

例如：

```text
空响应重试
pause/max_tokens 恢复
工具执行后回填 tool_result
awaitUser 结束 turn
max_steps 限制
```

### 32.2 工具失败不是系统失败

工具失败一般变成：

```python
ToolResult(ok=False, output="...")
```

然后作为：

```python
{"role": "tool_result", "isError": True}
```

回给模型。模型下一轮可以修正。

### 32.3 权限拒绝也是工具结果

权限拒绝通常会被工具执行层捕获成失败 ToolResult，反馈给模型，而不是直接杀掉进程。

### 32.4 MCP 工具和内置工具在 agent_loop 看来没有区别

MCP 工具最终也被包装成：

```python
ToolDefinition
```

所以 `run_agent_turn()` 只关心工具名和工具结果，不关心工具来源。

### 32.5 当前 TUI 只是 agent 外壳，不改变 agent 核心

当前 TUI 最终调用：

```python
run_agent_turn()
```

UI 负责：

```text
background thread
alternate screen
conversation 渲染
tool card 折叠/展开
pending approval UI
session autosave
鼠标/键盘事件
```

---

## 33. 推荐源码阅读顺序

不要按文件树读，按这个顺序读：

```text
1. cortexterm/types.py
2. cortexterm/main.py::main()
3. cortexterm/config.py::load_runtime_config()
4. cortexterm/prompt.py::build_system_prompt()
5. cortexterm/tools/__init__.py::create_default_tool_registry()
6. cortexterm/tooling.py::ToolRegistry.execute()
7. cortexterm/anthropic_adapter.py::AnthropicModelAdapter.next()
8. cortexterm/agent_loop.py::run_agent_turn()
9. cortexterm/permissions.py::PermissionManager
10. cortexterm/workspace.py::resolve_tool_path()
11. tools/read_file.py
12. tools/run_command.py
13. tools/edit_file.py
14. tools/patch_file.py
15. tools/ask_user.py
16. cortexterm/memory.py + tools/remember.py
17. cortexterm/mcp.py
18. cortexterm/skills.py + tools/load_skill.py
19. tty_app.py::run_tty_app()
20. cortexterm/tui/input.py
21. cortexterm/tui/transcript.py
22. cortexterm/session.py
23. cortexterm/context_manager.py
24. cortexterm/background_tasks.py
25. 扩展模块:
    state.py
    cost_tracker.py
    task_tracker.py
    hooks.py
    auto_mode.py
    async_context.py
    sub_agents.py
```

读完 1 到 10，你会懂 agent 主循环。  
读完 11 到 18，你会懂能力系统。  
读完 19 到 24，你会懂当前 TUI 和持久化。  
读完 25，你会懂扩展框架和未完全接入的设计。

---

## 34. 最短完整调用链

```text
main.py 启动
  ↓
读取配置 runtime
  ↓
发现 skills
  ↓
启动 MCP servers 并包装 MCP tools
  ↓
注册内置 tools + MCP tools
  ↓
初始化 PermissionManager
  ↓
选择 MockModelAdapter 或 AnthropicModelAdapter
  ↓
初始化 ContextManager / MemoryManager
  ↓
build_system_prompt()
  ↓
选择当前 TUI / stdin / 兼容终端模式
  ↓
用户输入
  ↓
刷新 system prompt 和 memory context
  ↓
messages.append(user)
  ↓
run_agent_turn()
  ↓
model.next(messages)
  ↓
如果 assistant:
    progress -> nudge 继续
    final/plain -> 结束 turn
    empty -> 重试/兜底
  ↓
如果 tool_calls:
    append assistant_tool_call
    tools.execute()
      validator
      run
      permissions / filesystem / command / network / MCP
    append tool_result
    回到 model.next()
  ↓
直到 final / ask_user / max_steps / error
  ↓
UI 展示
  ↓
history/transcript/session 保存
  ↓
退出时 tools.dispose() 关闭 MCP
```
