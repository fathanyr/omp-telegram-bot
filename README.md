# 🤖 OMP Telegram Bot

An interactive Telegram bot that exposes the **`omp` (Oh My Pi)** agentic coding CLI directly to your messaging app. Control your autonomous agent, navigate directories, manage Git branches, switch models with automatic fallback, and execute coding tasks from anywhere.

---

## Features

- 🚀 **Full OMP CLI Access**: Run agentic coding tasks, shell commands, file edits, and codebase questions directly through Telegram.
- 📂 **Directory Navigation**: Use `/cd` to hop between repositories or folders with instant path validation.
- 🌿 **Git Branch Management**: Inspect uncommitted changes with `/pwd`, list branches with `/branch`, and checkout or create branches via `/checkout <branch>`.
- 🧩 **Model Switching & Automatic Fallback**: Switch between available models on the fly with `/model <name>`. If a chosen model errors out, the runner automatically falls back to `openai-codex/gpt-5.6-luna` (or your configured `FALLBACK_MODEL`).
- 🛑 **Task Cancellation**: Cancel running tasks instantly with `/stop` — terminates the entire process tree cleanly without leaving orphaned processes.
- 🔄 **Session Continuity**: Multi-turn conversation context is preserved across prompts using omp session resume (`-r <session_id>`); reset anytime with `/reset`.
- ⚡ **Real-Time Progress Streaming**: Live throttled status messages report tool executions (`bash`, `read`, `edit`, `glob`, etc.) in real time.
- 🛡️ **Strict Access Control**: Only messages from your whitelisted Telegram user ID are processed; all unauthorized requests are immediately dropped.
- 🐳 **Docker-Ready**: Packaged with Docker Compose for single-command deployment with host user UID/GID mapping and volume mounts.

---

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Bot overview, current directory, active branch, session ID, and command menu |
| `/model` | Show current model, available catalog models, and fallback model |
| `/model <selector>` | Switch model (e.g. `/model Coding`, `/model openai-codex/gpt-5.6-luna`), or `/model default` |
| `/pwd` | Active directory, active Git branch, and short uncommitted git status (`git status -sb`) |
| `/cd <path>` | Switch working directory (supports `~`, relative, or absolute paths) |
| `/branch` | List local and remote Git branches sorted by commit recency |
| `/checkout <branch>` | Switch branch (fetches remotes first; supports `-b <new_branch>`) |
| `/status` | Check if omp is currently executing a task, elapsed time, and session ID |
| `/stop` | Abort the running task and kill all child processes immediately |
| `/reset` | Clear the session ID to start a completely fresh omp context |
| `<Any text prompt>` | Sends the text to `omp -p --auto-approve --mode json` in the active directory |

---

## Installation & Setup

### Prerequisites

- A Linux host with:
  - **Docker** and **Docker Compose** (recommended) OR **Python 3.12+** and **Git**
  - **`omp` CLI** installed (e.g., at `~/.local/bin/omp`) and authenticated with your AI providers
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
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

Edit `.env`:

```ini
# REQUIRED: Telegram Bot token from @BotFather
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

# REQUIRED: Your numeric Telegram user ID
ALLOWED_USER_ID=123456789

# OPTIONAL: Default working directory inside the container
DEFAULT_CWD=/workspace

# OPTIONAL: Path to omp CLI in container
OMP_BIN=/usr/local/bin/omp

# OPTIONAL: Fallback model if the selected model errors
FALLBACK_MODEL=openai-codex/gpt-5.6-luna

# OPTIONAL: Host paths for Docker mounts (defaults to standard locations)
HOST_OMP_BIN=~/.local/bin/omp
HOST_WORKSPACE_DIR=~
```

> **Security Note:** Never commit your `.env` file to version control. It is already added to `.gitignore`.

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

> **Important:** Only run **one** instance of the bot at a time (Docker OR Systemd, not both). Telegram allows only one polling connection per token; multiple instances will produce `409 Conflict` errors.

---

## Project Structure

```
omp-telegram-bot/
├── .env.example              # Template environment configuration
├── .gitignore                # Protects secrets, venv, and logs from git
├── .dockerignore             # Excludes secrets and caches from Docker image
├── Dockerfile                # Multi-stage container build with user mapping
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
