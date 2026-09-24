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
                └── Non-zero exit reports failure without blindly replaying work
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
| `stopped` | `bool` | Flag set by `/stop` for cancellation |
| `started_at` | `float \| None` | `time.monotonic()` start stamp of the running task |
| `lock` | `asyncio.Lock` | Serializes one omp task per user |

**Reset semantics**: `/cd`, `/checkout`, and `/model` selection (including `default`) clear `omp_session_id`. Context-changing commands are rejected during an active task; `/stop` remains available concurrently.

**Failure semantics**: Do not automatically replay failed work after possible tool side effects; report failure so the user can inspect the workspace and decide whether to retry.

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

Final answers escape untrusted HTML and render paired `**bold**` as Telegram HTML bold. A completed answer ending in a contiguous numbered list (1–8, two or more choices) receives inline buttons; callbacks authorize the user, validate an opaque per-session token and chat, and resume OMP with the selected number. The `-p` subprocess does not accept interactive stdin: choices are a subsequent session turn, not an interruption of a running task. Progress shows elapsed time and tool activity without raw thinking or tool arguments.

Tool summary line format: `🔧 <toolName>: <command|path|pattern|query|url|intent>` truncated to 120 chars.

---

## Command Interface

| Command | Agent Behavior |
|---|---|
| `/start`, `/help` | Report cwd, branch, session id, active model, and full command menu |
| `/pwd` | `git rev-parse --is-inside-work-tree`, `branch --show-current`, `status -sb` |
| `/cd <path>` | Resolve path within configured workspace boundary when set; validate directory, set cwd, clear session |
| `/branch` | List local and remote branches |
| `/checkout <branch>` | Checkout an existing branch without unconditional fetch; clear session |
| `/model` | List available models, show active selection and configured fallback |
| `/model <choice>` | Switch active model selector and clear session |
| `/model default` | Reset model selector and clear session |
| `/status` | Idle vs running (with elapsed time), cwd, session id, active model |
| `/stop` | Terminate the active OMP process group even while a prompt is running |
| `/reset` | Clear `omp_session_id` |
| `<plain text>` | Run `omp -p --auto-approve --mode json` in cwd |

---

## Operational Invariants

- **Single polling instance** per bot token; `drop_pending_updates=True` on startup.
- **Authorization first**: Require a decimal numeric configured user ID and a matching private chat before subprocesses run.
- **Secret isolation**: No credentials in tracked files or Docker build context; runtime credentials remain accessible to OMP.
- **No blind retry**: Non-zero exits do not trigger automatic replay of potentially side-effecting tasks.
- **Output sanitization**: Escape untrusted HTML and chunk Telegram messages within limits.
- **Process group isolation**: `start_new_session=True` allows `/stop` to terminate descendants.
- **Stream parsing**: Bytearray chunked buffer handles tool payloads >64KB safely.

---

## Deployment Topology

The bot can run containerized (`docker compose`) or via host `systemd`:

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `${HOST_OMP_BIN}` | `/usr/local/bin/omp` | ro | omp CLI binary (dynamically linked, glibc-compatible) |
| `${HOST_OMP_HOME}` | `/home/omp/.omp` | rw | omp config, model catalog, sessions, credentials |
| `${HOST_OMP_CONFIG}` | `/home/omp/.config/oh-my-pi` | ro | Provider definitions (`models.yaml`) |
| `${HOST_WORKSPACE_DIR}` | `/workspace` | rw | One explicitly selected repository or project directory |

Compose requires absolute host paths, maps host UID/GID, sets `WORKSPACE_ROOT=/workspace`, and does not grant blanket Git `safe.directory` trust. Host systemd runs as a non-root workspace owner with absolute host `OMP_BIN` and `DEFAULT_CWD`; set `WORKSPACE_ROOT` to confine `/cd` (without it, host navigation is unrestricted). The boundary restricts `/cd`, not OMP subprocess filesystem access. Only one polling instance may use a bot token.
