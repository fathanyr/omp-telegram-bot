# 🤖 OMP Telegram Bot

An interactive Telegram bot that exposes the **`omp` (Oh My Pi)** agentic coding CLI through your private chat. Navigate a configured workspace, switch Git branches and models, and run coding tasks remotely.

---

## Features

- 🚀 **Full OMP CLI Access**: Run agentic coding tasks, shell commands, file edits, and codebase questions directly through Telegram.
- 📂 **Directory Navigation**: Use `/cd` within the configured workspace boundary; host runs without a boundary do not restrict navigation.
- 🌿 **Git Branch & Push Management**: Inspect changes with `/pwd`, list branches with `/branch`, switch branches with `/checkout <branch>`, and push commits directly with `/push`.
- 🧩 **Model Switching**: Select a model with `/model <name>` or restore the CLI default with `/model default`; model changes start a fresh session. `FALLBACK_MODEL` identifies a configured alternative but failed work is not automatically replayed.
- 🛑 **Task Cancellation**: `/stop` requests termination of the active OMP process group. A stopped task is not retried.
- 🔄 **Session Continuity**: Prompts resume the current OMP session; `/reset` clears the session ID.
- ⚡ **Progress Streaming**: Throttled status messages summarize activity without exposing raw reasoning or tool arguments.
- 💬 **Readable Replies & Choices**: Completed numbered choices may appear as inline buttons that submit a follow-up turn; stale buttons do not run a task.
- 🛡️ **Access Control**: Only the configured numeric user ID in a private chat may control the bot. This does not sandbox OMP from files reachable by the service account.
- 🐳 **Docker-Ready**: Packaged with Docker Compose for single-command deployment with host user UID/GID mapping and volume mounts.

---

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Bot overview, current directory, active branch, session ID, and command menu |
| `/model` | Show current model, available catalog models, and fallback model |
| `/model <selector>` | Switch model (e.g. `/model Coding`, `/model openai-codex/gpt-5.6-luna`), or `/model default` |
| `/pwd` | Active directory, branch, and concise Git status |
| `/cd <path>` | Switch directory within the configured workspace boundary (relative or absolute path); starts a fresh session |
| `/branch` | List local and remote branches |
| `/checkout <branch>` | Switch to an existing branch; does not fetch remotes or create branches; starts a fresh session |
| `/push [remote] [branch]` | Push commits to the remote repository (auto-detects upstream or sets `-u origin <branch>`) |
| `/status` | Show active task, elapsed time, and session ID |
| `/stop` | Terminate the active task, even while a prompt is running |
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

Docker Compose builds the image, matches your host user UID/GID so mounted files retain proper ownership, and mounts your `omp` binary, credentials, and repositories:

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

For host operation, set `OMP_BIN` and `DEFAULT_CWD` to absolute host paths in `.env`; set `WORKSPACE_ROOT` to an absolute directory to confine `/cd`. Without `WORKSPACE_ROOT`, host `/cd` navigation is unrestricted. Run the service as a non-root user with access only to the intended workspace and OMP credentials. OMP uses auto-approval and can execute commands and modify any path accessible to that user; Telegram authorization and `/cd` confinement do not sandbox OMP subprocesses.

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

Edit the unit's `User`, `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` paths before enabling it. Ensure `.env` is owned by and readable only by the service user (`chmod 600 .env`), and the OMP executable and credentials are available to that same user. The unit uses `KillMode=control-group` so stopping the service terminates spawned tasks. Keep only one polling instance per token; stop the previous instance before switching between Compose and systemd. A restart clears in-memory bot session selections, and an interrupted OMP task is not automatically resumed.

While a task runs, `/stop` remains available; directory, model, branch, and session changes are rejected until the run ends. Failed tasks are not blindly replayed after possible side effects: inspect the workspace and decide whether to send a new prompt. `/checkout` changes to an existing branch without fetching; fetch updates separately in your workspace if needed. Inline choice buttons are follow-up turns, not interactive stdin to an in-progress process.

> **Important:** Run one polling instance per token (Docker **or** systemd, not both); concurrent polling produces Telegram `409 Conflict` errors.

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
