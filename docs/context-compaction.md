# Context compaction

CortexTerm provides two versioned compaction strategies:

| Strategy | Trigger | Behavior |
|---|---|---|
| `pi` (default) | estimated context tokens exceed `contextWindow - reserveTokens` | keep a recent token budget, summarize older history with a standalone model request, and rebuild model context as system prompt + summary + recent messages |
| `legacy` | estimated usage reaches 95% | delete old progress, tool, user, and assistant messages until usage is near 70% |

The legacy implementation is also preserved in Git by the `compaction-legacy-v1` tag. Pi-style development lives on the `codex/pi-style-compaction` branch until it is merged.

## Pi-style flow

Before every `model.next()` call, including calls after tool results are appended:

1. Reserve room for the next model response.
2. Walk backward through messages until `keepRecentTokens` is reached.
3. Choose a cut that does not leave a tool result without its tool call.
4. Send discarded history to `model.summarize()` with no tools attached.
5. Rebuild the provider context as base system prompt + structured summary + retained recent messages.
6. Store discarded raw messages in the summary message's `compactedMessages` field. The provider adapter ignores this field, while session JSON preserves it for replay and audit.

If the cut falls inside one unusually large turn, CortexTerm makes a separate summary for the removed turn prefix and retains its recent suffix. On later compactions, the previous summary is supplied to the summarizer and replaced by one updated checkpoint.

If summarization fails, CortexTerm emits `compact_failed`, keeps the original messages intact, and does not retry compaction again during that agent turn.

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

- `enabled`: enables automatic compaction.
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
