# CortexTerm 项目导学

> 项目定位：面向本地研发流程的 Python 终端 Coding Agent。本文以当前仓库源码为准，重点覆盖 Agent Loop、工具与 MCP、事件驱动 TUI、三级记忆、会话与上下文管理、Skill 渐进加载，以及 SWE-bench 生成与 Docker 官方判分。
>
> 个人职责口径：独立开发 Python 版 CortexTerm，负责整体架构、记忆系统、TUI 和 Docker 评测链路。`44/50（88%）` 为项目作者提供的 SWE-bench 结果，正式对外前应保留对应 predictions、官方汇总 JSON 与各实例 `report.json`。

## 1. 前置知识（面试高频标注）

| 知识点 | 为何需要 | 在本项目中的位置 | 高频度 |
| --- | --- | --- | --- |
| LLM Agent Loop | 理解模型如何在“回答”和“调用工具”之间循环 | `cortexterm/agent_loop.py`、`cortexterm/anthropic_adapter.py` | ★★★★★ |
| Tool Calling 与结构化协议 | 理解工具定义、参数校验、执行结果如何回填上下文 | `cortexterm/tooling.py`、`cortexterm/tools/` | ★★★★★ |
| 状态机与事件循环 | 理解终端输入、审批、滚动、执行完成等事件如何互斥处理 | `cortexterm/tty_app.py`、`cortexterm/tui/event_loop.py`、`cortexterm/tui/modes/` | ★★★★★ |
| 线程与同步原语 | 理解模型请求为何放后台线程，以及审批如何阻塞后台执行但不阻塞 UI | `cortexterm/tui/agent_turn.py`、`cortexterm/tty_app.py` | ★★★★☆ |
| ANSI 终端渲染 | 理解全屏刷新、窗口化 transcript、宽字符和滚动偏移 | `cortexterm/tui/screen.py`、`cortexterm/tui/transcript.py`、`cortexterm/tui/rendering.py` | ★★★★☆ |
| 分层持久化 | 理解用户、项目共享、项目本地三种知识的作用域 | `cortexterm/memory.py` | ★★★★★ |
| 原子写与会话恢复 | 防止进程中断后出现 0 字节或索引与正文不一致 | `cortexterm/session.py` | ★★★★☆ |
| 上下文窗口与压缩 | 长对话必须控制 token，且保留最近交互与工具因果链 | `cortexterm/context_manager.py`、`cortexterm/agent_loop.py` | ★★★★☆ |
| JSON-RPC 与 MCP | 理解外部工具如何发现、注册和调用 | `cortexterm/mcp.py`、`cortexterm/config.py` | ★★★★★ |
| 权限边界 | 区分路径访问、命令执行、文件编辑与 MCP 启动过滤 | `cortexterm/permissions.py`、各工具的权限检查 | ★★★★★ |
| Git worktree | 每个评测实例需要在指定基线提交上隔离修改 | `evals/run_swebench_harness.py` | ★★★★☆ |
| SWE-bench 判分语义 | 区分生成补丁、局部检查和官方 Docker resolved | `evals/run_swebench_harness.py`、`../../swebench-official-run/run_extra18_contaminated_eval.py` | ★★★★★ |

## 2. 重点亮点与学习顺序（先看这个）

| 亮点标题 | 为什么重要 | 通用技术关键词 | 先看哪些文件 | 建议学习顺序 |
| --- | --- | --- | --- | --- |
| 可扩展任务编排 | Coding Agent 的核心不是聊天，而是模型、工具、权限和上下文共同推进任务 | Agent Loop、Tool Calling、有限步数、错误恢复 | `cortexterm/agent_loop.py` → `cortexterm/tooling.py` → `cortexterm/anthropic_adapter.py` | 1 |
| 交互与执行解耦 | 模型与工具可能耗时数十秒，终端仍要响应输入、渲染进度和处理审批 | 主线程事件循环、后台线程、回调、同步事件 | `cortexterm/tty_app.py` → `cortexterm/tui/agent_turn.py` → `cortexterm/tui/event_loop.py` | 2 |
| 分层知识与会话连续性 | Agent 需要同时记住跨项目偏好、项目共识、本地隐私和当前对话 | 分层存储、作用域、原子写、自动保存、恢复 | `cortexterm/memory.py` → `cortexterm/tools/remember.py` → `cortexterm/session.py` | 3 |
| 开放协议扩展 | 内置工具之外，还要让不同 MCP Server 的能力动态进入统一注册表 | JSON-RPC、stdio、动态工具包装、资源和提示词 | `cortexterm/mcp.py` → `cortexterm/tools/__init__.py` → `cortexterm/config.py` | 4 |
| 渐进式能力加载 | Skill 不应全部塞进系统提示词，应先暴露摘要，再按需加载正文 | 元数据发现、按名加载、上下文预算、包边界 | `cortexterm/skills.py` → `cortexterm/prompt.py` → `cortexterm/tools/load_skill.py` | 5 |
| 可复现基准评测 | 生成补丁与官方判分必须隔离，否则容易泄漏测试补丁或误报 resolved | worktree、补丁审计、编译反馈、Docker harness | `evals/run_swebench_harness.py` → `../../swebench-official-run/run_extra18_contaminated_eval.py` | 6 |

### 建议先画出的总链路

```text
用户输入
  -> TUI 主线程解析事件
  -> 后台 Agent 线程调用 run_agent_turn
  -> 模型返回 assistant 或 tool_call
  -> ToolRegistry 校验并执行内置工具/MCP 工具
  -> tool_result 回填模型消息
  -> 重复直到最终回答、异常退出或达到步骤上限
  -> transcript 展示给用户，messages 保留模型所需的完整因果链
  -> session 自动保存；长期事实由 remember 写入分层 memory
```

## 3. 必备知识点

- [ ] 能解释 `assistant`、`assistant_progress`、`assistant_tool_call`、`tool_result` 在循环中的责任。
- [ ] 能说明 `ToolDefinition`、`ToolRegistry`、`ToolContext` 三者的区别。
- [ ] 能从一个 MCP 工具调用追到 `tools/call` JSON-RPC 请求和 stdout 响应匹配。
- [ ] 能说明 TUI 为什么要求主线程独占输入和渲染，耗时 Agent 工作放后台线程。
- [ ] 能区分模型上下文 `messages` 与用户可见 `transcript`，并说明为什么不能合并成同一份状态。
- [ ] 能说明用户、项目、local 三级记忆分别适合存什么，以及覆盖和注入顺序。
- [ ] 能解释 session 原子写、索引、自动保存和恢复的故障边界。
- [ ] 能说明上下文压缩触发阈值、保留策略与摘要信息损失风险。
- [ ] 能说明 Skill 的“发现摘要 → 按名加载正文”流程及当前 references 支持缺口。
- [ ] 能完整讲出 `problem_statement → base_commit → worktree → model_patch → 官方 Docker harness → test_patch → FAIL_TO_PASS/PASS_TO_PASS → resolved`。

## 4. 推荐阅读（结合仓库）

| 主题 | 通用技术点 | 建议阅读位置 | 预计时间 | 读完能回答什么 |
| --- | --- | --- | --- | --- |
| 程序启动 | 配置装配、依赖初始化、TTY/管道分流 | `cortexterm/main.py`、`cortexterm/config.py` | 25 分钟 | 从命令启动到第一次模型请求发生了什么 |
| Agent Loop | 模型步骤、工具回填、空响应与异常恢复 | `cortexterm/agent_loop.py`、`cortexterm/types.py` | 45 分钟 | Agent 为什么能连续调用多个工具并最终收口 |
| 模型适配 | 消息格式转换、HTTP 请求、重试与超时 | `cortexterm/anthropic_adapter.py`、`cortexterm/api_retry.py` | 35 分钟 | 模型协议如何与内部统一步骤对接 |
| 工具体系 | 注册表、输入校验、执行上下文、能力元数据 | `cortexterm/tooling.py`、`cortexterm/tools/__init__.py` | 40 分钟 | 新增一个工具需要接入哪些边界 |
| 权限系统 | 工作区边界、危险命令、编辑预览、持久授权 | `cortexterm/permissions.py`、`cortexterm/tools/run_command.py`、`cortexterm/tools/edit_file.py` | 45 分钟 | 什么时候会询问用户，授权粒度是什么 |
| TUI 主架构 | 输入、状态、渲染、执行副作用分层 | `docs/tui-architecture.md`、`cortexterm/tty_app.py`、`cortexterm/tui/state.py` | 45 分钟 | 为什么 TUI 不会因模型调用完全卡死 |
| TUI 渲染 | 节流重绘、窗口化 transcript、工具卡片 | `cortexterm/tui/rendering.py`、`cortexterm/tui/transcript.py`、`cortexterm/tui/tool_cards.py` | 40 分钟 | 长 transcript 如何避免每帧全量渲染 |
| 记忆系统 | 作用域、双格式持久化、上下文注入 | `cortexterm/memory.py`、`cortexterm/tools/remember.py` | 40 分钟 | 长期知识如何写入、加载和控制 token |
| 会话系统 | 数据模型、索引、原子写、自动保存 | `cortexterm/session.py`、`cortexterm/tui/session_lifecycle.py` | 35 分钟 | 意外退出后如何恢复消息和 transcript |
| 上下文治理 | token 估算、阈值、压缩与事件通知 | `cortexterm/context_manager.py`、`cortexterm/agent_loop.py` | 40 分钟 | 长会话如何避免直接撞上上下文上限 |
| Skill 加载 | 多根目录发现、去重优先级、按需加载 | `cortexterm/skills.py`、`cortexterm/prompt.py`、`cortexterm/tools/load_skill.py` | 30 分钟 | Skill 何时进入模型上下文，references 当前为何不可达 |
| MCP 接入 | 进程生命周期、协议探测、请求匹配、动态包装 | `cortexterm/mcp.py`、`.mcp.json` | 55 分钟 | 外部 MCP 工具如何变成普通 ToolDefinition |
| 评测生成 | 仓库缓存、worktree、补丁约束、修复反馈 | `evals/run_swebench_harness.py` | 60 分钟 | 如何批量生成可交给官方 harness 的补丁 |
| 官方判分 | Docker 环境、测试补丁、resolved 语义 | `../../swebench-official-run/run_extra18_contaminated_eval.py` | 30 分钟 | 为什么本地 pytest 通过不等于 SWE-bench resolved |

## 5. 自学提醒

若某文件或原理看不懂，请继续追问 AI；本技能负责给学习路径与题目，不提供逐行讲解。

## 6. 项目技术定位

**AI 工程 / 开发者工具 / 系统软件交叉项目。** 依据是：项目以 LLM Agent Loop 为核心，通过工具协议、权限控制、终端交互、长期记忆和可复现评测，将模型能力落成可以直接操作本地代码仓库的工程系统。

## 7. 核心原理解析

### 7.1 Agent Loop：把一次模型回答变成多步任务执行

**问题：** 单次问答无法完成“先读文件、再定位符号、修改代码、运行测试、根据失败继续修复”的闭环。

**机制：** `run_agent_turn` 每轮让模型基于当前消息产生下一步。当结果是工具调用时，经注册表完成参数校验与执行，并把 `tool_result` 追加回消息；当模型给出最终文本时才结束。循环还设置最大步骤、空响应恢复、thinking 中断恢复、网络与超时兜底，并通过回调将进度、工具开始和工具结果传给 TUI。

**项目落点：** `cortexterm/agent_loop.py:82` 是主循环；`cortexterm/tooling.py:123` 负责工具查找和统一执行；`cortexterm/anthropic_adapter.py:166` 把内部消息与模型接口互转。

### 7.2 TUI：用线程边界保证交互连续性

**问题：** 模型请求与命令执行是慢操作；若和终端输入、绘制共用一个同步流程，界面会冻结，审批和中断也无法及时处理。

**机制：** 主线程只负责读输入、路由状态和刷新界面；每个 Agent turn 在守护线程执行，通过回调更新 transcript 与工具卡片。渲染请求先进入节流器，由主事件循环 `flush`，避免后台线程直接写终端。权限请求用 `threading.Event` 在后台等待选择，主线程仍可绘制审批 UI。

**项目落点：** `cortexterm/tui/agent_turn.py:17` 启动后台执行；`cortexterm/tui/event_loop.py:19` 收割完成结果；`cortexterm/tui/rendering.py:10` 合并高频重绘；`cortexterm/tty_app.py:180` 装配整体运行时。

### 7.3 消息与展示状态分离：保住工具因果链

**问题：** 用户界面希望折叠长输出、限制可见窗口和生成摘要，但模型下一次请求必须看到原始工具调用及对应结果；直接拿 UI transcript 当上下文会破坏协议因果关系。

**机制：** `messages` 保存模型语义，包括系统提示词、用户消息、工具调用与工具结果；`transcript` 保存面向人的卡片、折叠状态、滚动偏移和显示摘要。后台 turn 只复制并更新 `messages`，回调只投影到 transcript，完成后由事件循环收割新的消息状态。

**项目落点：** `cortexterm/tty_app.py` 文件头明确这条边界；`cortexterm/tui/state.py` 定义展示状态；`cortexterm/tui/agent_turn.py` 完成两类状态的桥接。

### 7.4 分层记忆：按传播范围管理长期知识

**问题：** 用户偏好、项目共识和本机私有信息的共享范围不同，全部写到一个文件容易泄漏本地信息，也会让跨项目偏好重复维护。

**机制：** 记忆分为 user、project、local 三层，分别落到用户目录、项目共享目录和项目本地目录。`remember` 工具要求显式 scope，写入结构化 JSON 的同时生成可读 Markdown；启动时按 local、project、user 的顺序，在 token 预算内注入系统提示词。

**项目落点：** `cortexterm/memory.py:28` 定义作用域，`:191` 统一管理，`:346` 生成相关上下文；`cortexterm/tools/remember.py` 暴露模型工具；`cortexterm/main.py:238` 在启动阶段注入。

### 7.5 MCP：把外部协议能力包装为统一工具

**问题：** 如果每接一个外部工具都修改 Agent Loop，核心调度会和具体集成耦合，且无法复用 MCP 生态。

**机制：** 启动 MCP 子进程后先握手，再用 `tools/list` 获取描述，将每个远端工具包装成 `ToolDefinition`。执行时注册表调用闭包，闭包通过 `tools/call` 发送 JSON-RPC 请求；客户端用递增 id 和等待队列把 stdout 响应路由回对应请求，同时管理 stderr、超时和进程关闭。资源与提示词也会被包装为统一的列表和读取工具。

**项目落点：** `cortexterm/mcp.py:193` 是 stdio 客户端，`:401` 是请求匹配，`:500` 完成能力发现与动态注册，`cortexterm/tools/__init__.py:35` 合并内置和 MCP 工具。

### 7.6 SWE-bench：生成和判分两阶段隔离

**问题：** Agent 若在生成阶段看到 benchmark 的 `test_patch`，会造成评测泄漏；若只跑本地测试，又不能等价证明官方环境中的 resolved。

**机制：** 生成阶段按 `base_commit` 创建独立 worktree，只把问题描述交给 Agent，收集 `model_patch`，拒绝测试文件修改，并做无补丁重试与 Python 编译反馈。输出 predictions 后，再交给官方 SWE-bench Docker harness；官方阶段应用模型补丁与测试补丁，运行 `FAIL_TO_PASS` 和 `PASS_TO_PASS`，二者满足规则才标记 resolved。

**项目落点：** `evals/run_swebench_harness.py:680` 组织单实例生成，`:365` 审计非法补丁，`:443` 做编译检查；`../../swebench-official-run/run_extra18_contaminated_eval.py` 调用官方 `run_evaluation`。

## 8. 关键设计决策

| 决策 | 备选 | 当前取舍 | 风险 | 验证方式 |
| --- | --- | --- | --- | --- |
| 统一 Agent Loop | 为读、写、测试分别写固定工作流 | 用模型决定下一步，所有能力统一成工具 | 可能循环、空回答或反复失败 | 步骤上限、空响应重试、工具错误计数、端到端任务测试 |
| 主线程只管 TUI | 所有逻辑单线程串行 | Agent 后台线程，主线程输入与渲染 | 共享状态存在竞态；后台线程不能直接绘制 | 压力输入、审批并发、连续工具回调、Ctrl-C 测试 |
| transcript 与 messages 分离 | 直接复用一份消息列表 | 语义上下文和展示模型各自维护 | 映射遗漏会造成 UI 与模型状态不一致 | 恢复会话后核对工具链、折叠输出和最终上下文 |
| 三级记忆 | 单一全局文件或数据库 | 目录分层，JSON + Markdown 双格式 | 重复条目、覆盖优先级和敏感信息边界 | 同名事实冲突测试、作用域注入测试、token 截断测试 |
| 自动上下文压缩 | 超限后直接失败或仅删除旧消息 | 达阈值后摘要并保留最近消息 | 摘要会损失精确参数与失败上下文 | 构造长工具链，核对压缩前后任务连续性和 token 量 |
| 工具注册表统一扩展 | Agent Loop 对各工具写分支 | 内置工具与 MCP 工具共用定义和结果结构 | MCP 工具目前没有自动继承本地路径/命令权限语义 | 逐工具能力审计；将远端能力边界写入配置和文档 |
| Skill 渐进加载 | 启动时把全部 Skill 塞进系统提示词 | 系统提示只列摘要，命中后加载 `SKILL.md` | 当前 YAML 描述解析粗糙；安装仅复制单文件；references 无一等读取能力 | 增加前置元数据解析、整目录安装、受限相对路径资源读取测试 |
| 生成与官方评测隔离 | 生成时直接应用隐藏测试补丁 | 生成只产出模型补丁，Docker harness 独立判分 | predictions 与 report 不齐会导致指标无法审计 | 保存输入集、模型补丁、官方汇总和每实例 report，核对实例总数 |

### Skill 子系统的真实现状

当前实现不是把 Skill 包全部加载进模型上下文：

1. `discover_skills` 扫描项目和用户的 `.cortexterm/skills`，并兼容 `.claude/skills`，只把名称、描述、路径和来源放进系统提示词。
2. 模型判断命中某 Skill 后调用 `load_skill`，此时才把对应 `SKILL.md` 全文作为工具结果加入消息。
3. `install_skill` 当前只执行 `SKILL.md` 的单文件复制，未保留同目录 `references/`、`scripts/`、`assets/`。
4. `load_skill` 也没有返回稳定的 `skill_dir/plugin_root`，仓库里尚无 `read_skill_resource` 一类工具，因此 `SKILL.md` 中的相对引用并不能可靠按包边界读取。

建议的演进是：安装时复制完整 Skill 目录；发现阶段解析 YAML frontmatter；加载结果携带 Skill 根目录；新增只允许读取包根内相对路径的资源工具；脚本按工具或受控命令执行，而不是把整个 references/scripts 一次性塞入上下文。

## 9. 量化与验证（含待测，建议）

| 能力 | 当前证据 | 建议测法 | 对外口径 |
| --- | --- | --- | --- |
| 工具扩展 | 当前静态注册可枚举到 29 个内置工具，MCP 工具运行时追加 | 启动时记录内置/MCP 数量；做重复名、失联、超时和关闭测试 | 可写“集成 29 个内置工具并支持 MCP 动态扩展” |
| TUI 响应 | 架构有后台 Agent 线程和 16ms 最小重绘间隔 | 在连续流式输出、长工具结果、审批等待下记录输入延迟与刷新耗时 | 暂不写“性能提升百分比”，待测 P50/P95 |
| transcript 性能 | 代码按可见窗口渲染并缓存条目行 | 构造 1k/10k 条消息，比较窗口化前后的帧耗时和内存 | 可写“将长对话渲染复杂度约束到可见窗口”，具体耗时待测 |
| 记忆正确性 | 三级路径、显式 scope、JSON/Markdown 持久化已落地 | 验证跨项目、项目共享、本地私有三组读取矩阵及冲突规则 | 可写架构结果；不要声称召回率提升，除非补实验 |
| 会话可靠性 | 有临时文件 + 原子替换、索引和 30 秒自动保存 | 模拟保存中断、空会话、损坏索引、恢复最新会话 | 可写“支持自动保存与断点续聊”；故障恢复率待测 |
| 上下文治理 | 中英文估算、95% 阈值、压缩历史与 UI 事件已实现 | 构造长会话核对压缩前后 token、保留消息、任务成功率 | 可写“支持自动压缩”；准确率/节省比例待测 |
| Skill 完整性 | 已有发现与按需加载；包资源链路不完整 | 用含 `references/`、`scripts/` 的 Skill 做安装、读取和越界测试 | 对外只写“支持本地 Skill 发现与按需加载” |
| SWE-bench | 作者提供 44/50，即 88% | 固定数据集版本、模型配置、预算、run_id；归档 predictions、官方总报告和实例报告 | 证据齐备后写“50 个样本中解决 44 个（88%）” |
| 当前回归状态 | 本次项目前序检查曾得到 `212 passed, 6 failed`，失败含真实 API、记忆格式和工作区路径语义 | 修复或分类 6 个失败后再跑全量；将外部 API 测试与离线单测分组 | 不使用历史 README 的通过率替代当前测试结果 |

### 建议的最小验证顺序

1. 先跑离线单元测试，确认 Agent Loop、工具、TUI、memory/session 的纯逻辑。
2. 再跑 mock model 端到端流程，验证消息与 transcript 映射、权限审批和会话保存。
3. 单独跑真实模型 API 冒烟，避免网络或账号问题污染离线回归结论。
4. 对 Skill 包做整目录安装、相对引用读取、`..` 越界拒绝三类测试。
5. 对 MCP 做 tools/resources/prompts 三类发现，以及超时、子进程退出和资源清理测试。
6. SWE-bench 先产出 predictions，再用官方 Docker harness 判分；最终按 `resolved_instances` 与每实例 `report.json` 对账。

## 10. 一小时源码速通路线

```text
0-10 分钟   main.py：看依赖如何装配，记住 messages、tools、permissions、memory
10-25 分钟  agent_loop.py + tooling.py：画出模型/工具循环
25-37 分钟  tty_app.py + tui/agent_turn.py：理解主线程/后台线程边界
37-47 分钟  memory.py + session.py：比较长期知识与当前会话
47-55 分钟  mcp.py + skills.py：比较协议扩展与知识型扩展
55-60 分钟  run_swebench_harness.py：讲清生成与官方判分隔离
```

完成后应能用三句话概括项目：CortexTerm 是一个终端 Coding Agent；核心由统一 Agent Loop 驱动内置和 MCP 工具，并用事件驱动 TUI 承载长任务；三级记忆、会话恢复、上下文压缩和官方 SWE-bench Docker 评测共同解决持续使用与可验证性问题。
