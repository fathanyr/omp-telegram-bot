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
        │       ├── Working Directory (cwd, prev_cwd)
        │       ├── Active Model Selector & Thinking Level
        │       └── Resumed OMP Session ID
        │
        ├── Command Dispatcher (/cd, /pwd, /diff, /log, /branch, /checkout, /pull, /push, /model, /thinking, /status, /stop, /reset)
        │
        └── Prompt Runner (run_omp)
                │
                ├── Subprocess: omp -p --auto-approve --mode json [-r session_id] [--model model] [--thinking thinking] -- "<prompt>"
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
| `prev_cwd` | `str \| None` | Previous working directory for `/cd -` return |
| `model` | `str \| None` | Custom model selector; `None` uses omp default |
| `thinking` | `str \| None` | Custom thinking level (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `auto`); `None` uses omp default |
| `omp_session_id` | `str \| None` | Resumed omp session ID; `None` starts a fresh session |
| `proc` | `Process \| None` | Currently running omp or git subprocess handle |
| `stopped` | `bool` | Flag set by `/stop` for cancellation |
| `started_at` | `float \| None` | `time.monotonic()` start stamp of the running task |
| `lock` | `asyncio.Lock` | Serializes one omp task per user |
| `busy` | `bool` | Set by prompt runs, `/push`, `/pull`, and `/checkout`; context-changing commands reject while true |
| `task` | `str \| None` | Human-readable label of the active tracked task for `/status` |
| `generation` | `int` | Incremented on every context reset; invalidates stale inline-choice tokens |
| `choice` | `tuple \| None` | Pending inline numbered-choice token tuple `(token, chat_id, session_id, options)` |
| `model_choice` | `tuple \| None` | Pending inline model menu token tuple `(token, chat_id, options)` |

**Reset semantics**: `/cd`, `/checkout`, `/model` selection (including `default`), and `/thinking` clear `omp_session_id`, bump `generation`, and clear all pending choice and model menus via `clear_choices()`. Context-changing commands are rejected during an active task; `/stop` remains available concurrently. Git operations (`/push`, `/pull`, `/diff`, `/log`) do not reset session context.

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

Final answers escape untrusted HTML, then render fenced code blocks (with a sanitized `language-*` class), inline `` `code` `` spans, and paired `**bold**` runs. Bold markers must hug non-space text and may not straddle another `**` pair, so `2 ** 3` and `**kwargs` stay literal. An unclosed fence is closed at the end of the message so truncated output still renders as code. A completed answer ending in a contiguous numbered list (1–8, two or more choices) receives inline buttons; callbacks authorize the user, validate an opaque per-session token and chat, and resume OMP with the selected number. The `-p` subprocess does not accept interactive stdin: choices are a subsequent session turn, not an interruption of a running task. Progress shows elapsed time and tool activity without raw thinking or tool arguments.

Inline model menus use the same token discipline under the `model:` callback prefix: `model_menu()` stores `(token, chat_id, {index: selector})` in `model_choice`, and `on_model_choice()` rejects expired or foreign-chat callbacks before switching the selector.

Tool summary line format: `🔧 <toolName>: <command|path|pattern|query|url|intent>` truncated to 120 chars.

---

## Command Interface

| Command | Agent Behavior |
|---|---|
| `/start`, `/help` | Report cwd, branch, session id, active model, thinking level, and full command menu |
| `/pwd` | `git rev-parse --is-inside-work-tree`, `branch --show-current`, `status -sb` |
| `/cd <path>` | Resolve path within configured workspace boundary when set; validate directory, set cwd, update prev_cwd, clear session |
| `/cd -` | Switch back to the previous working directory (`prev_cwd`) and clear session |
| `/diff [staged]` | Working tree or staged diff with `--stat` summary; truncated at 12,000 characters |
| `/log [n]` | Show last `n` commits (1–50, default 10) in `--oneline` format |
| `/branch` | List local and remote branches sorted by committer date |
| `/checkout <branch>` | Checkout an existing branch without unconditional fetch; clear session |
| `/pull [remote] [branch]` | Fast-forward pull commits from remote (`--ff-only` default, supports `--rebase` / `--no-rebase`); tracked for `/stop` |
| `/push [remote] [branch]` | Push commits to the remote repository (auto-detects upstream or sets `-u origin <branch>`); batches authentication; tracked for `/stop` |
| `/model` | List available models, show active selection and configured fallback; presents inline keyboard picker |
| `/model <choice>` | Switch active model selector and clear session |
| `/model default` | Reset model selector to CLI default and clear session |
| `/thinking` | Show active thinking level and list available levels |
| `/thinking <level>` | Set thinking level (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `auto`) and clear session |
| `/thinking default` | Reset thinking level to CLI default and clear session |
| `/status` | Idle (with cwd, branch, model, thinking, session id) vs running task (with task name and elapsed time) |
| `/stop` | Terminate the active OMP or Git process group even while a task is running |
| `/reset` | Clear `omp_session_id` and all pending inline menus |
| `<plain text>` | Run `omp -p --auto-approve --mode json` in cwd with active model and thinking flags |

---

## Operational Invariants

- **Single polling instance** per bot token; `drop_pending_updates=True` on startup.
- **Authorization first**: Require a decimal numeric configured user ID and a matching private chat before subprocesses run.
- **Secret isolation**: No credentials in tracked files or Docker build context; runtime credentials remain accessible to OMP.
- **No blind retry**: Non-zero exits do not trigger automatic replay of potentially side-effecting tasks.
- **Output sanitization**: Escape untrusted HTML and chunk Telegram messages within limits; fences, inline code, and bold are rendered from escaped text only.
- **Process group isolation**: `start_new_session=True` allows `/stop` to terminate descendants; `terminate()` sends SIGTERM to the group and escalates to SIGKILL after a 2-second grace period.
- **Bounded Git network operations**: `/push`, `/pull`, and `/checkout` run with `GIT_TIMEOUT` (180s) and `track=sess`, so a hung remote cannot hold the session busy forever and `/stop` can abort it.
- **Stream parsing**: Bytearray chunked buffer handles tool payloads >64KB safely.
- **GitHub authority via environment**: `git_env()` injects `GITHUB_TOKEN`, commit identity, `GIT_TERMINAL_PROMPT=0`, and `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` through `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` so credentials never touch disk; the same environment is passed to every `git()` call and to the `omp` subprocess.

---

## Deployment Topology

The bot can run containerized (`docker compose`) or via host `systemd`:

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `${HOST_OMP_BIN}` | `/usr/local/bin/omp` | ro | omp CLI binary (dynamically linked, glibc-compatible) |
| `${HOST_OMP_HOME}` | `/home/omp/.omp` | rw | omp config, model catalog, sessions, credentials |
| `${HOST_OMP_CONFIG}` | `/home/omp/.config/oh-my-pi` | ro | Provider definitions (`models.yaml`) |
| `${HOST_WORKSPACE_DIR}` | `/workspace` | rw | One explicitly selected repository or project directory |
| `${HOST_SSH_DIR}` | `/home/omp/.ssh` | ro | Host SSH keys (`id_ed25519`/`id_rsa`) and `known_hosts` for Git SSH remotes |
| `/var/run/docker.sock` | `/var/run/docker.sock` | rw | Host Docker daemon socket |
| `/` | `/host` | rw | Whole host filesystem for file operations and path inspection |
| `${HOST_WORKSPACE_DIR}` | `/home/ubuntu` | rw | Host workspace path identity |

Compose requires absolute host paths, maps host UID/GID, sets `WORKSPACE_ROOT=/workspace`, and does not grant blanket Git `safe.directory` trust. Host systemd runs as a non-root workspace owner with absolute host `OMP_BIN` and `DEFAULT_CWD`; set `WORKSPACE_ROOT` to confine `/cd` (without it, host navigation is unrestricted). The boundary restricts `/cd`, not OMP subprocess filesystem access. Only one polling instance may use a bot token.

**Host privilege model**: both Docker and bare-metal deployments are deliberately wired for host administration, so neither forms a security boundary.
- **Bare-metal systemd**: runs unnamespaced as the service account; group memberships and `sudoers` rules define reach.
- **Docker Compose**: uses `privileged: true`, `pid: host`, `network_mode: host`, `/var/run/docker.sock`, and `/host`. The image bakes an unprivileged bot user with passwordless `sudo`, group membership for the host docker GID, a `host-exec` helper that enters host PID 1 namespaces via `nsenter -t 1 -m -u -i -n -p --`, and same-named shims (`systemctl`, `journalctl`, `apt-get`, `service`, `ufw`, `ss`, `apt`). Host loopback services (`127.0.0.1:8090`, `127.0.0.1:20128`) are directly reachable.
Neither deployment sandboxes the agent from the host; both represent remote root execution through Telegram.

**omp CLI upgrades**: `HOST_OMP_BIN` is mounted read-only, so `omp update` runs on the host, never inside the container. `OMP_BIN` is read once at startup, and a running container keeps the old binary inode even after the host file is replaced; apply an upgrade with a host-side `omp update` followed by `docker compose restart omp-bot` (or `docker compose up -d` when the install path itself changed) or `systemctl restart omp-bot`. Restarts clear in-memory session state, so they MUST NOT interrupt an active task. Rebuild the image only when `bot.py`, `Dockerfile`, or `requirements.txt` change; if a release alters the parsed flags or JSON events, update the repository first.
