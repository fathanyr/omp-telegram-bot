# Product Requirements Document (PRD): OMP Telegram Bot

## 1. Executive Summary
The **OMP Telegram Bot** is a secure, interactive interface bridging the Telegram messaging platform to `omp` (Oh My Pi agentic coding CLI). It allows authorized developers to control their agent, inspect and switch Git branches, navigate workspace repositories, execute complex code modifications, and manage agent models directly from mobile or desktop Telegram clients.

---

## 2. Security & Access Model
- **Whitelist Enforcement**: Every command and prompt handler strictly validates `effective_user.id == ALLOWED_USER_ID`. Unauthorized users are rejected before any agent, shell, or filesystem logic is invoked.
- **Credential Isolation**: All sensitive values (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_ID`, file paths) are injected strictly via environment variables (`.env`). No credentials or tokens exist in source code.
- **Process Isolation**: The bot spawns `omp` in its own process group (`start_new_session=True`), ensuring clean cancellation via `/stop` without orphaned child processes.
- **Container Isolation**: In Docker deployments, the container runs under a configurable unprivileged user matching the host UID/GID to keep mounted repository permissions intact.

---

## 3. Core Functional Capabilities

### 3.1 Model Selection & Automatic Fallback
- `/model`: Lists all available models from `omp models --json` along with the currently active selection and designated fallback.
- `/model <selector>`: Switches model (e.g. `Coding`, `openai-codex/gpt-5.6-luna`), invalidating stale session context to start fresh on the new model.
- `/model default`: Clears override and returns to default CLI configuration.
- **Automatic Fallback Invariant**: If a non-fallback model execution exits non-zero, the bot automatically retries the prompt with `FALLBACK_MODEL` (`openai-codex/gpt-5.6-luna` by default), alerting the user in chat.
- **Stop Safety**: An explicit `/stop` command sets an internal cancellation flag to ensure a killed task is never mistakenly retried by the fallback mechanism.

### 3.2 Directory Navigation & Workspace Tracking
- `/cd <path>`: Changes working directory with full support for relative paths, `~`, and absolute paths. Validates directory existence and resets directory-scoped OMP session state.
- `/pwd`: Displays active directory, branch name, detached HEAD status, and concise Git status (`git status -sb`).

### 3.3 Git Branch Management
- `/branch`: Lists local and remote branches sorted by recent commit date.
- `/checkout <branch>`: Fetches latest remotes and switches branch; supports flags like `-b` to create new branches.

### 3.4 Process Control & Session Continuity
- `/status`: Shows whether OMP is idle or currently executing, with run time and session ID.
- `/stop`: Aborts any running task immediately via `SIGTERM` followed by `SIGKILL` and process-group cleanup (`pkill -9 -g <pgid>`).
- `/reset`: Clears the session identifier, forcing the next prompt to start a fresh agent session.
- **Session Continuity**: Multi-turn conversation context is automatically preserved across prompts in the same directory using `-r <session_id>`.
- **Readable interaction**: Render paired `**bold**` from OMP as escaped Telegram HTML; show compact status without raw thinking/tool arguments. Completed numbered-choice questions offer authorized inline buttons that submit the selected number as a follow-up session turn. Stale choices must not launch a subprocess.

---

## 4. Technical Stack & Deployment
- **Runtime**: Python 3.12+ (Docker container or local virtual environment).
- **Framework**: `python-telegram-bot` v22+ (pure `asyncio`).
- **Containerization (Recommended)**: Docker Compose mounting the host omp binary, credentials, and host workspace.
- **Host Supervision (Alternative)**: `systemd` service template (`omp-bot.service.example`).
- **Output Sanitization**: Strips ANSI terminal escape codes; HTML entity escaping for Telegram HTML formatting; throttled real-time status updates (1.5s interval).
- **Chunked JSON Parser**: `iter_json_lines()` bytearray reader safely handles large tool outputs (>64KB) without buffer overruns.
