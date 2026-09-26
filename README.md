# 🤖 OMP Telegram Bot

An interactive Telegram bot that exposes the **`omp` (Oh My Pi)** agentic coding CLI through your private chat. Navigate a configured workspace, switch Git branches and models, and run coding tasks remotely.

---

## Features

- 🚀 **Full OMP CLI Access**: Run agentic coding tasks, shell commands, file edits, and codebase questions directly through Telegram.
- 📂 **Directory Navigation**: Use `/cd <path>` within the configured workspace boundary, or `/cd -` to return to the previous working directory; host runs without a boundary do not restrict navigation.
- 🌿 **Git Workspace & History**: Inspect status with `/pwd`, view recent commits with `/log [n]`, view unstaged or staged diffs with `/diff [staged]`, list branches with `/branch`, switch branches with `/checkout <branch>`, fast-forward pull from remotes with `/pull`, and push commits with `/push`.
- 🧩 **Model & Thinking Control**: Select a model via `/model` with an interactive inline button picker or `/model <selector>`, restore CLI default with `/model default`, and tune reasoning depth with `/thinking [level]`. Model and thinking changes start a fresh session cleanly.
- 🛑 **Task Cancellation**: `/stop` requests immediate termination of the active OMP or Git process group. A stopped task is not retried.
- 🔄 **Session Continuity**: Prompts resume the current OMP session; `/reset` clears the session ID.
- ⚡ **Progress Streaming**: Throttled status messages summarize activity without exposing raw reasoning or tool arguments.
- 💬 **Readable Replies & Choices**: Completed numbered choices may appear as inline buttons that submit a follow-up turn; stale buttons do not run a task.
- 🛡️ **Access Control**: Only the configured numeric user ID in a private chat may control the bot. This does not sandbox OMP from files reachable by the service account.
- 🐳 **Docker-Ready**: Packaged with Docker Compose for single-command deployment with host user UID/GID mapping and volume mounts.
---

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Bot overview, current directory, active branch, session ID, model, thinking level, and command menu |
| `/model` | Show current model, available catalog models, and fallback model with an interactive inline keyboard picker |
| `/model <selector>` | Switch model (e.g. `/model Coding`, `/model openai-codex/gpt-5.6-luna`), or `/model default` |
| `/thinking` | Show current thinking level and available levels (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `auto`) |
| `/thinking <level>` | Set agent thinking level, or `/thinking default` to reset |
| `/pwd` | Active directory, branch, and concise Git status |
| `/cd <path>` | Switch directory within the configured workspace boundary (relative or absolute path); `/cd -` returns to previous directory; starts a fresh session |
| `/diff [staged]` | View working tree diff (with `--stat` summary) or staged changes |
| `/log [n]` | View recent one-line commits (default 10, max 50) |
| `/branch` | List local and remote branches sorted by committer date |
| `/checkout <branch>` | Switch to an existing branch; does not fetch remotes or create branches; starts a fresh session |
| `/pull [remote] [branch]` | Fast-forward pull commits from the remote repository (`--ff-only` by default) |
| `/push [remote] [branch]` | Push commits to the remote repository (auto-detects upstream or sets `-u origin <branch>`) |
| `/status` | Show active task name, elapsed time, and session ID (or idle status with model and thinking settings) |
| `/stop` | Terminate the active task process group, even while a prompt or git network operation is running |
| `/reset` | Start a fresh OMP session |
| `<Any text prompt>` | Run `omp -p --auto-approve --mode json` in the active directory |

---

## Installation & Setup

### Prerequisites

- A Linux host with:
  - **Docker** and **Docker Compose** (recommended) OR **Python 3.12+** and **Git**
  - **`omp` CLI** installed (e.g., at `~/.local/bin/omp`) and authenticated with your AI providers
- Your Telegram numeric user ID (get it from [@userinfobot](https://t.me/userinfobot))

---

### Step 1: Clone the Repository

```bash
git clone https://github.com/fathanyr/omp-telegram-bot.git
cd omp-telegram-bot
```

---

### Step 2: Configure Environment Variables

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with a real token and decimal numeric `ALLOWED_USER_ID`; only that user's private chat is accepted. For Docker, replace all `/home/youruser/...` placeholders with existing absolute host paths (never `~`). `HOST_OMP_BIN` is the executable, `HOST_OMP_HOME` stores OMP sessions/authentication, `HOST_OMP_CONFIG` contains provider definitions, `HOST_SSH_DIR` optionally mounts the host SSH keys for Git remote access, and `HOST_WORKSPACE_DIR` is the one intended project directory, not your home directory. Compose mounts that directory at `/workspace` and sets `WORKSPACE_ROOT=/workspace` for `/cd`.

To enable GitHub pushes (`/push` or agent Git operations), configure `GITHUB_TOKEN` (for token authentication over HTTPS) or `HOST_SSH_DIR` (for host SSH key authentication with `git@github.com:` remotes). Optionally specify `GIT_USER_NAME` and `GIT_USER_EMAIL` for Git commit authorship.

Set `HOST_UID` and `HOST_GID` to the workspace owner's IDs; Compose fixes `DEFAULT_CWD=/workspace` and `OMP_BIN=/usr/local/bin/omp`. `FALLBACK_MODEL` is optional.

```bash
id -u
id -g
chmod 600 .env
```

Keep `.env` and provider credentials private. `.dockerignore` sends only `bot.py`, `requirements.txt`, and `Dockerfile` to the build context, but runtime bind mounts still expose their contents to OMP. Do not mount sensitive directories or repositories you do not intend the agent to modify.

---

### Step 3: Run the Bot

#### Option A: Run with Docker Compose (Recommended)

Docker Compose builds the image, matches your host user UID/GID so mounted files retain proper ownership, and mounts your `omp` binary, credentials, and repositories. The container is **not** a sandbox: it runs with host control enabled so the agent can administer the machine it was deployed on.

```bash
# Build and start in background
docker compose up -d

# Check status
docker compose ps

# View live logs
docker compose logs -f

# Stop the bot
docker compose down
```

The mounted OMP binary must run on the image's Linux architecture and glibc; a host executable linked against unavailable libraries will not work. Container Git trusts repositories owned by the mapped UID, **not every path**. If Git reports dubious ownership, correct the workspace ownership or explicitly configure `safe.directory` for that specific trusted repository as the container user; do not use `safe.directory '*'`.

##### Host control inside the container

The service is built for host administration, so it deliberately gives up isolation:

| Setting | Effect |
|---|---|
| `privileged: true` + `pid: host` | Host PID namespace reachable, so `nsenter -t 1` enters the real root filesystem, systemd, and process tree |
| `network_mode: host` | Shares host loopback; `127.0.0.1:8090` (beszel), `127.0.0.1:20128` (9router), and other loopback-only services resolve with no port publishing |
| `/var/run/docker.sock` | Bundled `docker` CLI drives host containers directly |
| `/:/host` | Whole host filesystem readable as `/host/...` by the agent's file tools |
| `HOST_WORKSPACE_DIR:/home/ubuntu` | Host home mounted at its real path, so absolute paths refer to the same file inside and outside the container |
| Same-named shims | `systemctl`, `journalctl`, `apt-get`, `service`, `ufw`, `ss`, and `apt` run against the host; `host-exec` runs any host command |

`systemctl status nginx` inside the container shows the host's nginx. `host-exec <cmd>` runs `<cmd>` in the host namespaces as root via the passwordless `sudo` rule baked into the image. The bot user (`omp`, matching `HOST_UID`/`HOST_GID`) is a member of the `HOST_DOCKER_GID` group, so the 0660 socket needs no sudo.

**This is not a security boundary.** The agent can do anything the host user can, plus root. Mount only what you intend the agent to modify, and treat the deployment as remote root access to the host.

Mount host paths explicitly — Compose does not expand `~`. `.dockerignore` sends only `bot.py`, `requirements.txt`, and `Dockerfile` to the build context, but runtime bind mounts still expose their contents to OMP.

> **Important:** stop and disable any host `systemd` instance before starting the container, and never run both — two pollers on one token produce Telegram `409 Conflict` errors:
>
> ```bash
> sudo systemctl disable --now omp-bot
> ```

#### Option B: Run on Bare Metal (Systemd / Local Venv)

If you prefer to run directly on the host:

```bash
# 1. Create and activate a Python 3.12 virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Test run
python bot.py
```

For host operation, set `OMP_BIN` and `DEFAULT_CWD` to absolute host paths in `.env`; set `WORKSPACE_ROOT` to an absolute directory to confine `/cd`. Without `WORKSPACE_ROOT`, host `/cd` navigation is unrestricted. Run the service as a non-root user with access only to the intended workspace and OMP credentials. OMP uses auto-approval and can execute commands and modify any path accessible to that user; Telegram authorization and `/cd` confinement do not sandbox OMP subprocesses. The service account's own privileges are the ceiling: because host runs are not namespaced, the agent sees host `docker`, `systemctl`, `sudo`, host processes, and loopback-only services exactly as that user does — add it to the `docker` group or grant narrow `sudoers` rules only if you intend the agent to use them.

To run as a system service with auto-restart on boot:

```bash
# Copy and edit the example systemd unit
sudo cp omp-bot.service.example /etc/systemd/system/omp-bot.service
sudo nano /etc/systemd/system/omp-bot.service   # adjust User and paths

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now omp-bot

# View logs
journalctl -u omp-bot -f
```

Edit the unit's `User`, `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` paths before enabling it. Ensure `.env` is owned by and readable only by the service user (`chmod 600 .env`), and the OMP executable and credentials are available to that same user. The unit orders after `network-online.target` and `docker.service`, sets `PYTHONUNBUFFERED=1` for clean journal logging, and uses `KillMode=control-group` so stopping the service terminates spawned tasks. Keep only one polling instance per token; stop the previous instance before switching between Compose and systemd. A restart clears in-memory bot session selections, and an interrupted OMP task is not automatically resumed.

While a task runs, `/stop` remains available; directory, model, thinking-level, branch, and session changes are rejected until the run ends. Failed tasks are not blindly replayed after possible side effects: inspect the workspace and decide whether to send a new prompt. `/checkout` changes to an existing branch without fetching; `/pull` defaults to `--ff-only` and `/push` auto-detects upstream, so fetch or rebase explicitly in your workspace when history has diverged. Inline choice buttons are follow-up turns, not interactive stdin to an in-progress process.

> **Important:** Run one polling instance per token (Docker **or** systemd, not both); concurrent polling produces Telegram `409 Conflict` errors.

### Upgrading the OMP CLI

The bot never bundles `omp`: Docker bind-mounts your host binary (`HOST_OMP_BIN` → `/usr/local/bin/omp:ro`), and host runs execute `OMP_BIN` directly. Upgrading omp is a host binary update plus a bot restart — no image rebuild unless `bot.py`, `Dockerfile`, or `requirements.txt` changed.

```bash
# 1. Let the running task finish (or /stop it): a restart kills in-flight
#    tasks and clears in-memory session/model selections.
omp update            # or `omp update --check` to preview first
omp --version

# 2. Docker Compose deployment
docker compose restart omp-bot
docker exec omp-telegram-bot /usr/local/bin/omp --version   # verify

# 2. Host / systemd deployment
sudo systemctl restart omp-bot
journalctl -u omp-bot -n 5
```

`OMP_BIN` is resolved once at bot startup, so a restart is required even though the path is unchanged. If an omp update changes its install path, update `HOST_OMP_BIN`/`OMP_BIN` in `.env` and run `docker compose up -d` (recreate, not just restart). Sessions in `HOST_OMP_HOME` survive updates; only major version jumps may make very old sessions unreadable for `-r` resume. If a release changes the JSON event shapes or CLI flags the bot parses (`-p --auto-approve --mode json`, `--model`, `--thinking`, `-r`, `models --json`, or the `session`/`message_*`/`tool_execution_*`/`agent_end` events), update this repo (`git pull` + `docker compose up -d --build`) before restarting.

---

## Project Structure

```
omp-telegram-bot/
├── .env.example              # Template environment configuration
├── .gitignore                # Protects secrets, venv, and logs from git
├── .dockerignore             # Excludes secrets and caches from Docker image
├── Dockerfile                # Minimal runtime image with host UID/GID mapping
├── docker-compose.yml        # Service definition with volume mounts
├── requirements.txt          # Python dependencies (python-telegram-bot, python-dotenv)
├── bot.py                    # Core bot logic, streaming parser, and commands
├── omp-bot.service.example   # Systemd service unit template
├── README.md                 # User guide and setup instructions
├── AGENTS.md                 # Agent architecture and event specifications
├── PRD.md                    # Product requirements and invariant definitions
└── RULES.md                  # Development and doc sync standards
```

---

## Maintenance & Contributions

Follow the rules defined in `RULES.md`:
1. Every new feature, command, or parameter must update `README.md`, `AGENTS.md`, and `PRD.md` in lockstep.
2. All secrets must remain in `.env` and never be committed.
3. Every handler must enforce user authorization.
4. Process termination (`/stop`) must kill the entire process group without triggering fallback loops.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
