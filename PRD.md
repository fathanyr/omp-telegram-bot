# Product Requirements Document (PRD): OMP Telegram Bot

## 1. Executive Summary
The **OMP Telegram Bot** is a secure, interactive interface bridging the Telegram messaging platform to `omp` (Oh My Pi agentic coding CLI). It allows authorized developers to control their agent, inspect and switch Git branches, navigate workspace repositories, execute complex code modifications, and manage agent models directly from mobile or desktop Telegram clients.

---

## 2. Security & Access Model
- **Whitelist Enforcement**: Require decimal numeric `ALLOWED_USER_ID` and accept only its private Telegram chat before launching subprocesses.
- **Credential Isolation**: Keep Telegram tokens, GitHub tokens, and OMP credentials outside tracked files and the Docker build context; the runtime OMP process can read mounted credentials. GitHub credentials/authority are injected into subprocesses through non-persisted Git environment variables.
- **Process Isolation**: Run OMP in its own process group so `/stop` can terminate the active task concurrently.
- **Container Scope**: Run under the workspace owner's unprivileged UID/GID, mount only the intended workspace, and do not globally trust Git repositories. Auto-approved OMP commands can still access everything reachable by the runtime account.

---

## 3. Core Functional Capabilities

### 3.1 Model Selection & Failure Handling
- `/model`: Lists available models and shows the active selector and configured fallback.
- `/model <selector>`: Switches model and invalidates stale session context.
- `/model default`: Clears the override and session ID, returning to the CLI default.
- **No blind replay**: If execution fails after potential tool side effects, report failure rather than automatically retrying a prompt with another model.
- **Stop Safety**: `/stop` remains responsive during a running prompt and does not launch another run.

### 3.2 Directory Navigation & Workspace Tracking
- `/cd <path>`: Changes directory within `WORKSPACE_ROOT` when set, validates it, and clears the session ID. Compose sets the root to `/workspace`; without a host `WORKSPACE_ROOT`, host navigation is unrestricted. This command restriction does not sandbox OMP.
- `/pwd`: Displays active directory, branch name, detached HEAD status, and concise Git status (`git status -sb`).

### 3.3 Git Branch Management
- `/branch`: Lists local and remote branches sorted by recent commit date.
- `/checkout <branch>`: Switches to an existing branch without unconditional fetch or branch-creation flags.
- `/push [remote] [branch]`: Pushes local commits to the configured remote repository. Automatically detects upstream tracking branch, or defaults to `-u origin <branch>` when no tracking branch is configured. Operates non-interactively with `BatchMode=yes` and `GIT_TERMINAL_PROMPT=0`.

### 3.4 Process Control & Session Continuity
- `/status`: Shows whether OMP is idle or currently executing, with run time and session ID.
- `/stop`: Terminates the active OMP process group even while a prompt is running; context changes are rejected during execution.
- `/reset`: Clears the session identifier, forcing the next prompt to start a fresh agent session.
- **Session Continuity**: Multi-turn conversation context is automatically preserved across prompts in the same directory using `-r <session_id>`.
- **Readable interaction**: Render paired `**bold**` from OMP as escaped Telegram HTML; show compact status without raw thinking/tool arguments. Completed numbered-choice questions offer authorized inline buttons that submit the selected number as a follow-up session turn. Stale choices must not launch a subprocess.

---

## 4. Technical Stack & Deployment
- **Runtime**: Python 3.12+ (Docker container or local virtual environment).
- **Framework**: `python-telegram-bot` v22+ (pure `asyncio`).
- **Containerization (Recommended)**: Docker Compose requires explicit absolute paths for the OMP binary, credentials, narrow writable workspace, and optional host SSH key directory (`/home/omp/.ssh:ro`). Includes `openssh-client` and Git configuration injection via `GIT_CONFIG_KEY_<n>`.
- **Host Supervision (Alternative)**: `systemd` template runs as the non-root workspace owner with explicit absolute host paths and control-group termination.
- **Output Sanitization**: Strips ANSI terminal escape codes; HTML entity escaping for Telegram HTML formatting; throttled real-time status updates (1.5s interval).
- **Chunked JSON Parser**: `iter_json_lines()` bytearray reader safely handles large tool outputs (>64KB) without buffer overruns.
