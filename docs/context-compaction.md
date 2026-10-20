# Context compaction

CortexTerm provides two versioned compaction strategies:

| Strategy | Trigger | Behavior |
|---|---|---|
| `pi` (default) | estimated context tokens exceed `contextWindow - reserveTokens` | keep a recent token budget, summarize older history with a standalone model request, and rebuild model context as system prompt + summary + recent messages |
| `legacy` | estimated usage reaches 95% | delete old progress, tool, user, and assistant messages until usage is near 70% |

The legacy implementation is also preserved in Git by the `compaction-legacy-v1` tag.

## Pi-style flow

Before every `model.next()` call, including calls after tool results are appended:

1. Reserve room for the next model response.
2. Walk backward through messages until `keepRecentTokens` is reached.
3. Choose a cut that does not leave a tool result without its tool call.
4. Send discarded history to `model.summarize()` with no tools attached.
5. Build a candidate context as base system prompt + structured summary + retained recent messages. Accept it only if the summary is nonempty, the context shrank, and its estimated tokens fit the input budget.
6. Store removed raw messages in the summary message's `compactedMessages` field. The provider adapter ignores this field, while session JSON preserves it for replay, audit, and retrieval with `history_search` / `history_read`.

If the cut falls inside one unusually large turn, CortexTerm makes a separate summary for the removed turn prefix and retains its recent suffix. On later compactions, the previous summary is supplied to the summarizer and replaced by one updated checkpoint.

## Failure recovery and request guard

Compaction gets at most **three attempts** before the next `model.next()` call. A summary request error, empty summary, or candidate that still exceeds the budget is a failed attempt. A split-turn attempt may make two summary requests; the provider's HTTP retries are separate from this limit.

- Failed attempts preserve the active messages, previous archive, and compaction history. A partially completed split-turn summary is not committed.
- `compact_retry` shows retry progress in the TUI. If no safe cut exists, retrying the same boundary would not help, so it fails immediately without a summary request.
- After the final failure, `compact_failed` and `context_blocked` report the pause. The next model request is **not sent**. The normal turn-completion path returns control to the input box.
- There is no automatic fallback to deletion. Check `/context`, the summary service, and the compaction settings before asking the agent to continue. Lowering `keepRecentTokens` may help an oversized retained suffix, but cannot split an indivisible message/tool group.

The guard also runs when automatic compaction is disabled: disabling compression does not permit a context already known to exceed the estimated input budget. Pi uses `contextWindow - reserveTokens`; legacy retains its 95% threshold. Legacy deletions are still available only when that strategy is explicitly configured.

This guard uses the existing heuristic **message** token estimate, not a provider tokenizer or an exact accounting of the entire request (including tool schemas and protocol overhead). It prevents known oversized requests; it cannot guarantee that the provider will never reject a request for its own token limit.

## Retrieving original messages after compaction

These are model-callable tools, not slash commands. For example, ask: “Find the full test error from earlier in this conversation, before compaction.” The model can then call:

```json
{"query": "FAILED", "role": "tool_result", "tool_name": "run_command", "limit": 5}
```

`history_search` performs case-insensitive literal search over archived message text, tool inputs, and tool names. An empty query browses the archive. It returns chronological matches, `archive:N` message IDs, snippets of up to 400 characters, and `next_offset` for pagination. `offset` counts matching messages; `limit` defaults to 5 and is capped at 20.

Then call `history_read` with a returned ID:

```json
{"message_id": "archive:3", "offset": 0, "limit": 4000}
```

`history_read` returns the original text in character ranges (default 4,000, maximum 8,000 per call). Follow `next_offset` to read the rest. Tool-call text is its JSON input; metadata includes the original tool name/ID and links to archived calls/results with the same tool ID.

Scope and persistence:

- Only the **current conversation's Pi archive** is searchable. Before its first compaction, the archive is normally empty. Other sessions and long-term memory are not searched.
- IDs are positions in the cumulative archive. They stay stable across repeated Pi compaction and saving/resuming the same session. They are not global IDs.
- The archive continues to use the existing session save/autosave mechanism; these tools do not introduce another on-disk database or change save timing.
- Retrieved text is returned inside the new history tool result. Old tool calls are never reinserted as live calls, and their commands are not executed again.
- Text is historical evidence and may be outdated. Legacy deletion cannot recover messages that it already discarded.

## Configuration

Add `compaction` to `~/.cortexterm/settings.json`:

```json
{
  "compaction": {
    "enabled": true,
    "strategy": "pi",
    "reserveTokens": 16384,
    "keepRecentTokens": 20000
  }
}
```

- `enabled`: enables automatic compaction; the estimated input-budget guard remains active when disabled.
- `strategy`: `pi` or `legacy`.
- `reserveTokens`: space reserved before the context window limit for the next response.
- `keepRecentTokens`: approximate amount of recent conversation retained verbatim by the Pi strategy.

To temporarily restore the original behavior:

```json
{
  "compaction": {
    "strategy": "legacy"
  }
}
```
