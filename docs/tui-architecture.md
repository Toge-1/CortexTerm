# CortexTerm 当前 TUI 架构

这份文档只描述当前正在使用的 TUI，不再保留“旧 TUI / 新 TUI”之类的混合叙述。

默认入口链路：

```text
cortexterm.main
  -> cortexterm.tty_app.run_tty_app(...)
```

这里的 TUI 指运行在终端里的交互式界面。它不是普通 `input()` 循环，而是进入 raw mode 后自己接收键盘、鼠标、滚轮事件，再根据状态重绘屏幕。

## 1. 总体分层

当前 TUI 按下面几层拆分：

```text
tty_app.py
  入口装配：创建 args/state、接权限审批桥、安装屏幕生命周期、分发事件

tui/session_lifecycle.py
  session 选择、恢复、ScreenState 初始化、退出时最终保存

tui/event_loop.py
  raw terminal 主循环：autosave tick、读取输入 chunk、解析事件、收后台 agent 结果

tui/runtime.py
  用户提交 prompt 后的输入路由：slash 命令、本地工具快捷调用、启动 agent turn

tui/agent_turn.py
  后台 agent turn 生命周期：刷新 system prompt、启动线程、接 model/tool loop 回调

tui/agent_tool_events.py
  工具调用的展示状态机：tool start/result、聚合编辑进度、错误 hint、自动折叠

tui/modes/
  normal / approval / read 模式下的键盘鼠标事件处理

tui/render_session.py + tui/render_home.py
  session 页面和启动页渲染

tui/tool_cards.py + tui/transcript.py + tui/transcript_ops.py
  transcript 条目、工具卡片折叠/展开、保存和展示摘要
```

## 2. 运行链路

一次启动大致是：

```text
run_tty_app(...)
  -> bootstrap_screen_state(...)
  -> permissions.prompt = permission bridge
  -> enter alternate screen / hide cursor
  -> render_screen(...)
  -> run_terminal_event_loop(...)
  -> save_final_session(...)
```

`tty_app.py` 现在主要是装配层。它保留部分 `_xxx` 兼容导出，是为了不破坏现有测试和外部调用点。

## 3. 主线程和后台线程

主线程负责用户能直接感受到的交互：

```text
1. 检查 autosave
2. 检查后台 agent turn 是否完成
3. 非阻塞读取终端输入
4. parse_input_chunk(...) 解析成事件
5. 按当前 mode 分发事件
6. flush 渲染
```

慢任务不放在主线程跑。用户提交普通 prompt 后，`tui/agent_turn.py` 会启动 daemon 线程调用 `run_agent_turn(...)`。这样模型请求、工具执行、测试命令不会卡死终端输入和滚动。

## 4. 消息状态和展示状态分离

`args.messages` 是给模型看的真实上下文；`ScreenState.transcript` 是给用户看的展示流。

这两个不能混为一谈：

```text
args.messages
  保留完整语义：user、assistant、tool call、tool result、final answer 等

ScreenState.transcript
  面向终端展示：可以折叠工具结果、显示摘要、隐藏冗长文件内容、追加状态提示
```

所以工具输出可以在 TUI 里折叠，但模型上下文不能因为展示折叠而丢失内容。

## 5. 输入事件分发

终端原始输入链路：

```text
raw bytes/chars
  -> parse_input_chunk(...)
  -> TextEvent / KeyEvent / MouseEvent / WheelEvent
  -> _handle_event(...)
  -> 当前 mode handler
```

当前 mode 优先级：

```text
Ctrl-C
  -> pending approval mode
  -> mouse tool-card hit
  -> transcript read mode
  -> normal mode
```

审批模式会抢占普通输入，因为工具正在等待用户授权。读模式会让滚轮和 PageUp/PageDown 优先控制 transcript。

## 6. 用户提交 prompt 后发生什么

`normal` 模式按 Enter 后调用 `runtime.handle_input(...)`：

```text
空输入
  -> 忽略

/exit
  -> 退出

/tools, /debug, 其他本地命令
  -> 直接写 transcript

本地工具快捷命令
  -> execute_tool_shortcut(...)

普通自然语言 prompt
  -> start_agent_turn(...)
```

`start_agent_turn(...)` 会：

```text
1. 把用户输入写入 transcript
2. 标记 busy / Thinking
3. 刷新 system prompt，带上 permissions、skills、MCP servers、memory context
4. 把 user message 追加到 args.messages
5. 后台线程调用 run_agent_turn(...)
6. 通过 callback 把 assistant/progress/tool/context 事件同步到 transcript
7. 线程结束后把新 messages 放到 state.agent_result
8. 主循环下一次 tick 收割结果，更新 args.messages
```

## 7. 工具调用怎么展示

工具相关展示由 `tui/agent_tool_events.py` 和 `tui/tool_cards.py` 共同负责。

工具开始：

```text
on_tool_start(...)
  -> handle_tool_start(...)
  -> push_transcript_entry(kind="tool", status="running")
```

工具完成：

```text
on_tool_result(...)
  -> handle_tool_result(...)
  -> update_tool_entry(...)
  -> schedule_tool_auto_collapse(...)
```

文件编辑类工具会按 `tool_name:path` 做聚合，避免连续编辑同一个文件时刷出一串重复卡片。

## 8. 权限审批和 ask user 的区别

权限审批不是普通问答。它是工具执行链路里的安全暂停点。

```text
工具准备执行危险操作
  -> PermissionManager 调用 permissions.prompt(...)
  -> TUI 设置 state.pending_approval
  -> 主线程渲染审批 UI
  -> 用户选择 allow/deny
  -> agent 线程继续执行或收到拒绝结果
```

因此 approval 有独立 mode 和独立状态，不走普通 prompt 输入。

## 9. 当前文件职责表

| 文件 | 职责 |
| --- | --- |
| `cortexterm/tty_app.py` | 入口装配、权限审批桥、屏幕生命周期、事件分发兼容层 |
| `cortexterm/tui/state.py` | TUI 状态数据结构 |
| `cortexterm/tui/event_loop.py` | raw terminal 主循环 |
| `cortexterm/tui/session_lifecycle.py` | session 恢复和保存 |
| `cortexterm/tui/runtime.py` | 用户提交输入后的路由 |
| `cortexterm/tui/agent_turn.py` | 后台 agent turn 生命周期 |
| `cortexterm/tui/agent_tool_events.py` | 工具卡片状态机 |
| `cortexterm/tui/modes/normal.py` | 普通输入模式 |
| `cortexterm/tui/modes/approval.py` | 权限审批和反馈模式 |
| `cortexterm/tui/modes/read.py` | transcript 阅读模式 |
| `cortexterm/tui/render_home.py` | 启动页 |
| `cortexterm/tui/render_session.py` | session 页面 |
| `cortexterm/tui/tool_cards.py` | 工具卡片创建、更新、折叠、鼠标命中 |
| `cortexterm/tui/tool_display.py` | 工具输入/输出摘要 |
| `cortexterm/tui/transcript.py` | transcript 渲染和窗口裁剪 |
| `cortexterm/tui/transcript_ops.py` | transcript 保存和历史格式化 |
| `cortexterm/tui/terminal.py` | raw mode 和跨平台按键读取 |

## 10. 后续还能继续优化的点

当前拆分已经让入口层可读，但仍有几个可以继续优化的方向：

1. 后台线程 callback 现在会直接改 `ScreenState`。更理想的方式是后台线程只投递 UI event，由主线程统一消费并修改状态。
2. `agent_tool_events.py` 还可以继续拆成“聚合编辑状态机”和“普通工具卡片完成逻辑”，但现在它至少已经从 agent turn 生命周期里分离出来。
3. 渲染层还可以继续按 header/footer/prompt/transcript 拆细，不过目前 `render_session.py` 的职责仍然比较集中。
4. 兼容导出还留在 `tty_app.py`，这是为了不破坏测试；等测试迁移到新模块后可以进一步减少入口层符号。
