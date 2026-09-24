# AGENTS Specification: OMP Telegram Bot

This document defines the agent architecture, execution flow, state model, and invariants for the OMP Telegram Bot.

---

## Architecture

```
Telegram User (ID whitelist)
        │
        │ Telegram Bot API (long polling: getUpdates)
        ▼
   bot.py Application (python-telegram-bot v22+, asyncio)
        │
        ├── Session State Router (per authorized user_id)
        │       ├── Working Directory (cwd)
        │       ├── Active Model Selector
        │       └── Resumed OMP Session ID
        │
        ├── Command Dispatcher (/cd, /pwd, /branch, /checkout, /model, /stop, /reset)
        │
        └── Prompt Runner (run_omp)
                │
                ├── Subprocess: omp -p --auto-approve --mode json [-r session_id] [--model model] "<prompt>"
                │
                ├── pump_stdout() parses JSONL events → StreamState
                │        └── throttled status edit (1.5s) + typing action
                │
                ├── final_text_from_events() → chunked reply
                │
                └── Auto-Fallback Handler (retries with FALLBACK_MODEL on non-zero exit)
                        ▲
                        └─ Suppressed if task was terminated via /stop
```

---

## State Model

`USER_SESSIONS[user_id]` (in-memory, per authorized user):

| Field | Type | Meaning |
|---|---|---|
| `cwd` | `str` | Active working directory for all omp + git subprocesses |
| `model` | `str \| None` | Custom model selector; `None` uses omp default |
| `omp_session_id` | `str \| None` | Resumed omp session ID; `None` starts a fresh session |
| `proc` | `Process \| None` | Currently running omp subprocess handle |
| `stopped` | `bool` | Flag set by `/stop` to prevent fallback retries |
| `started_at` | `float \| None` | `time.monotonic()` start stamp of the running task |
| `lock` | `asyncio.Lock` | Serializes one omp task per user |

**Reset semantics**: `/cd`, `/checkout`, and `/model <choice>` reset `omp_session_id = None` because agent context is directory-, branch-, and model-scoped.

**Fallback invariant**: If execution exits non-zero, `model != FALLBACK_MODEL`, and `stopped == False`, the runner automatically retries fresh with `FALLBACK_MODEL`.

---

## OMP Event Handling Contract

`omp --mode json` emits one JSON object per stdout line. Handled events:

| Event | Action |
|---|---|
| `session` | Capture `id` → `state.session_id` (persisted for `-r` resume) |
| `message_start` (assistant) | Capture `thinking` / `text` parts |
| `message_update` (`thinking_delta`, `text_delta`) | Append to live stream buffers |
| `tool_execution_start` | Append one-line tool summary to the status message |
| `tool_execution_end` (`isError`) | Append `⚠️ <tool> failed` marker |
| `agent_end` | Authoritative final assistant text source |

Tool summary line format: `🔧 <toolName>: <command|path|pattern|query|url|intent>` truncated to 120 chars.

---

## Command Interface

| Command | Agent Behavior |
|---|---|
| `/start`, `/help` | Report cwd, branch, session id, active model, and full command menu |
| `/pwd` | `git rev-parse --is-inside-work-tree`, `branch --show-current`, `status -sb` |
| `/cd <path>` | Resolve `~`/relative/absolute, validate dir, set cwd, clear session |
| `/branch` | `git branch -a --sort=-committerdate` (top 60) |
| `/checkout <branch>` | `git fetch --all --prune` then `git checkout <args>`; clear session |
| `/model` | List available models, show active selection and fallback |
| `/model <choice>` | Switch active model selector, clear session, auto-fallback enabled |
| `/model default` | Reset model selector to omp default |
| `/status` | Idle vs running (with elapsed time), cwd, session id, active model |
| `/stop` | `SIGTERM` → 0.7s grace → `SIGKILL` → `pkill -9 -g <pgid>`, set `stopped=True` |
| `/reset` | Clear `omp_session_id` |
| `<plain text>` | Run `omp -p --auto-approve --mode json` in cwd |

---

## Operational Invariants

- **Single polling instance** per bot token; `drop_pending_updates=True` on startup.
- **Authorization first** in every handler; unauthorized users are rejected before any subprocess runs.
- **Secret isolation**: No credentials or private IDs in tracked files.
- **Model fallback**: Any non-zero exit on a non-fallback model retries with `FALLBACK_MODEL` unless user invoked `/stop`.
- **Output sanitization**: ANSI escapes stripped, HTML escaped, chunking on line boundaries under 4000 chars.
- **Process group isolation**: `start_new_session=True` ensures `/stop` kills the entire descendant process tree.
- **Stream parsing**: Bytearray chunked buffer handles tool payloads >64KB safely.

---

## Deployment Topology

The bot can run containerized (`docker compose`) or via host `systemd`:

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `${HOST_OMP_BIN}` | `/usr/local/bin/omp` | ro | omp CLI binary (dynamically linked, glibc-compatible) |
| `${HOST_OMP_HOME}` | `/home/omp/.omp` | rw | omp config, model catalog, sessions, credentials |
| `${HOST_OMP_CONFIG}` | `/home/omp/.config/oh-my-pi` | rw | Provider definitions (`models.yaml`) |
| `${HOST_WORKSPACE_DIR}` | `/workspace` | rw | Repositories the agent inspects and modifies |
