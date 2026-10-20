# Session ownership

A local conversation can be edited by only one CortexTerm instance at a time.
Starting a second instance with the same `--resume <session_id>` (or with
`--resume` selecting that same latest session) reports:

```text
Session '<id>' is already in use by another CortexTerm instance.
Close that instance before resuming, or start without --resume to create a new session.
```

The second instance does not restore the conversation into its editable state,
start the TUI, or run an agent turn. Different sessions remain independent.

## Ownership lifetime

```text
Select the session ID
  -> acquire its nonblocking OS file lock
  -> load the current JSON while holding the lock
  -> run the TUI and autosave using the same lock
  -> save the final snapshot
  -> release the lock in finally
```

Acquiring ownership only around each save would still allow two instances to
load the same old snapshot and later overwrite each other. Interactive sessions
hold ownership for their entire lifetime, starting before loading editable data.

`SessionLock` uses `msvcrt.locking(..., LK_NBLCK, 1)` on Windows and
`fcntl.flock(..., LOCK_EX | LOCK_NB)` on Unix. These are standard-library APIs;
no Redis server or third-party Python package is required. Acquisition fails
immediately when another owner holds the lock. See the
[Python Windows locking reference](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)
and [Unix locking reference](https://docs.python.org/3/library/fcntl.html#fcntl.flock).

Ownership is held on the separate, stable file
`~/.cortexterm/sessions/.<session_id>.json.lock`. The session JSON still uses
temporary-file writes followed by atomic replacement. A lock on the JSON itself
would not remain associated with the replacement file on Unix.

The lock file is deliberately **not deleted** on release. Its existence does
not mean that a session is occupied: the OS tracks the open handle's lock. After
a crash or process termination, ownership is released by the OS. Do not delete
lock files to force access to a running session; on Unix this could create two
independently locked files with the same pathname.

## Saves, deletion, and API callers

- `ScreenState` owns the live lock. `AutosaveManager` and the final-save path use
  that same lock rather than trying to acquire it a second time.
- Initialization, terminal setup, event-loop, and final-save exceptions release
  ownership. Release occurs after the final-save attempt, not before it.
- `save_session()` without a supplied lock attempts a short exclusive lock and
  returns `False` if the session is currently owned. A supplied lock must still
  be held by this process and must belong to this session.
- `delete_session()` and old-session cleanup skip sessions that are in use.
- Read-only listing and `load_session()` remain possible. They return a disk
  snapshot, without automatically following the running instance's changes.

Code that will edit a loaded session must acquire ownership **before** loading,
and pass it to every save:

```python
lock = acquire_session_lock(session_id)
try:
    session = load_session(session_id)
    if session is not None:
        # Edit this session while ownership is held.
        save_session(session, session_lock=lock)
finally:
    lock.release()
```

A short save lock alone does not detect an old snapshot loaded before ownership
was acquired. There is no automatic conversation merge or revision conflict
resolution.

## Scope

This coordinates cooperating CortexTerm instances using the same local session
directory. It is not a multi-user collaboration service or a distributed lock
for multiple machines. Programs that bypass these APIs, including older
CortexTerm versions, can still modify the JSON.

Session ownership does not lock repository files. Two different sessions can
still modify the same working directory. Use separate worktrees when workspace
isolation is needed. Process termination releases ownership but does not save
unsaved conversation changes or roll back previously executed tools.

The regression tests exercise competing real processes and forced termination,
plus save/delete rejection, autosave, and TUI cleanup on normal and failed exits.
