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

### 3.1 Model Selection, Thinking Control & Failure Handling
- `/model`: Lists available models, shows the active selector and configured fallback, and presents an inline keyboard picker of the first page of catalog selectors.
- `/model <selector>`: Switches model and invalidates stale session context.
- `/model default`: Clears the override and session ID, returning to the CLI default.
- `/thinking`: Shows the active thinking level and the supported levels (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `auto`).
- `/thinking <level>`: Sets the reasoning level passed as `--thinking <level>` to each OMP run and clears session context; `/thinking default` removes the override.
- **No blind replay**: If execution fails after potential tool side effects, report failure rather than automatically retrying a prompt with another model.
- **Stop Safety**: `/stop` remains responsive during a running prompt and does not launch another run.

### 3.2 Directory Navigation & Workspace Tracking
- `/cd <path>`: Changes directory within `WORKSPACE_ROOT` when set, validates it, and clears the session ID. Compose sets the root to `/workspace`; without a host `WORKSPACE_ROOT`, host navigation is unrestricted. This command restriction does not sandbox OMP.
- `/cd -`: Returns to the previously recorded working directory (`prev_cwd`) and clears the session ID.
- `/pwd`: Displays active directory, branch name, detached HEAD status, and concise Git status (`git status -sb`).

### 3.3 Git Workspace Inspection, Branch Management & Remotes
- `/diff [staged]`: Shows a `--stat` summary plus the full working-tree diff, or the staged diff with `staged`/`--staged`/`--cached`. Output is truncated at 12,000 characters to stay within Telegram limits.
- `/log [n]`: Shows the last `n` commits (`--oneline`), defaulting to 10 and capped at 50.
- `/branch`: Lists local and remote branches sorted by recent commit date.
- `/checkout <branch>`, `/pull`, and `/push` run in their own process groups with a 180-second network timeout, are tracked on the session, and can be aborted with `/stop`.
- `/checkout <branch>`: Switches to an existing branch without unconditional fetch or branch-creation flags.
- `/pull [remote] [branch]`: Pulls from the remote using `--ff-only` by default so a divergent history fails fast instead of blocking on an interactive merge; `--rebase` and `--no-rebase` are accepted explicitly.
- `/push [remote] [branch]`: Pushes local commits to the configured remote repository. Automatically detects upstream tracking branch, or defaults to `-u origin <branch>` when no tracking branch is configured. Operates non-interactively with `BatchMode=yes` and `GIT_TERMINAL_PROMPT=0`.
- **No remote fetch on branch switch**: `/checkout` never fetches; use `/pull` or an agent prompt when remote updates are required.

### 3.4 Process Control & Session Continuity
- `/status`: Reports the active task name and elapsed time while running, or the idle working directory, branch, model, thinking level, and session ID.
- `/stop`: Terminates the active OMP or Git process group (SIGTERM to the group, then SIGKILL after a 2-second grace period) even while a prompt is running; context changes are rejected during execution.
- `/reset`: Clears the session identifier, forcing the next prompt to start a fresh agent session.
- **Session Continuity**: Multi-turn conversation context is automatically preserved across prompts in the same directory using `-r <session_id>`.
- **Prompt isolation**: The prompt is passed after a `--` delimiter so user text beginning with `-` cannot be parsed as an OMP flag.
- **Readable interaction**: Render paired `**bold**`, inline code spans, and fenced code blocks from OMP as escaped Telegram HTML; show compact status without raw thinking/tool arguments. Completed numbered-choice questions offer authorized inline buttons that submit the selected number as a follow-up session turn. Stale choices must not launch a subprocess.

---

## 4. Technical Stack & Deployment
- **Runtime**: Python 3.12+ (Docker container or local virtual environment).
- **Framework**: `python-telegram-bot` v22+ (pure `asyncio`).
- **Containerization (Recommended)**: Docker Compose requires explicit absolute paths for the OMP binary, credentials, narrow writable workspace, and optional host SSH key directory (`/home/omp/.ssh:ro`). Includes `openssh-client` and Git configuration injection via `GIT_CONFIG_KEY_<n>`. Compose grants host control by design: `privileged: true`, `pid: host`, `network_mode: host`, a mounted `/var/run/docker.sock`, the host filesystem at `/host`, and the workspace at its host path. The image provides an unprivileged bot user with passwordless `sudo`, membership in the host docker GID, a `host-exec` helper that enters host PID 1 namespaces via `nsenter`, and same-named shims so `systemctl`, `journalctl`, `apt-get`, `service`, `ufw`, `ss`, and `apt` execute against the host. The container is therefore not a sandbox and MUST NOT be described as one.
- **Host Supervision (Alternative)**: `systemd` template runs as the non-root workspace owner with explicit absolute host paths, `After=network-online.target docker.service`, `PYTHONUNBUFFERED=1`, and control-group termination. Host runs are not namespaced: the service account's group memberships and `sudoers` rules define what the agent can reach (docker socket, `systemctl`, host `/proc`, loopback services), and no sandboxing directives are set, since they would defeat host administration.
- **Output Sanitization**: Strips ANSI terminal escape codes; HTML entity escaping for Telegram HTML formatting; throttled real-time status updates (1.5s interval).
- **Chunked JSON Parser**: `iter_json_lines()` bytearray reader safely handles large tool outputs (>64KB) without buffer overruns.
- **omp Upgrades**: The bot never bundles `omp`; Docker bind-mounts the host binary and host runs use `OMP_BIN`. Replacing the host binary requires restarting the bot before the new version is used, must not interrupt an active task, and needs a repo update only when a release changes the flags or JSON events the bot parses.
