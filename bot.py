"""OMP Telegram Bot.

Bridges the local `omp` agentic CLI to Telegram with workspace navigation and
Git branch control. See README.md / AGENTS.md / PRD.md / RULES.md.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import signal
import subprocess
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("omp-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
OMP_BIN = os.getenv("OMP_BIN") or os.path.expanduser("~/.local/bin/omp")
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT") or ("/workspace" if os.path.isdir("/workspace") and os.getenv("DEFAULT_CWD") == "/workspace" else None)

TG_LIMIT = 4000
STATUS_EDIT_INTERVAL = 1.5
MAX_TOOL_LINES = 8
MAX_EVENT_LINE = 1024 * 1024
MAX_EVENTS = 16
MAX_STREAM_TEXT = 65536
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "openai-codex/gpt-5.6-luna")

# GitHub authority, supplied through .env: a personal access token for HTTPS
# remotes and/or an SSH directory (mounted read-only in Docker) for
# git@github.com: remotes. GIT_USER_* supply the commit identity, which an
# ephemeral container has no other source for.
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or "").strip()
GITHUB_USERNAME = (os.getenv("GITHUB_USERNAME") or "x-access-token").strip() or "x-access-token"
GIT_USER_NAME = (os.getenv("GIT_USER_NAME") or "").strip()
GIT_USER_EMAIL = (os.getenv("GIT_USER_EMAIL") or "").strip()

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CHOICE_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")
# Bold markers must hug non-space text and must not straddle another `**`
# (so `2 ** 3`, `**kwargs`, and `**kwargs ... **bold**` stay literal).
BOLD_RE = re.compile(r"(?<!\*)\*\*(?!\s)((?:(?!\*\*).)+?)(?<!\s)\*\*(?!\*)", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
FENCE_RE = re.compile(r"(?m)^[ \t]*```([^\n`]*)\n(.*?)(?:^[ \t]*```[^\n]*$|\Z)", re.DOTALL)
PUSH_FLAGS = {"-u", "--set-upstream", "--force-with-lease", "--dry-run", "--tags"}
PUSH_REF_RE = re.compile(r"^[a-zA-Z0-9_.\-/]+$")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")
GIT_TIMEOUT = 180.0
DIFF_LIMIT = 12000
LOG_DEFAULT = 10
LOG_MAX = 50
MODEL_PAGE_SIZE = 16


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def clean_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").strip()


def render_bold(text: str) -> str:
    """Escape text and translate paired `**bold**` runs to Telegram HTML."""
    parts = BOLD_RE.split(text)
    return "".join(f"<b>{esc(part)}</b>" if index % 2 else esc(part)
                   for index, part in enumerate(parts))


def render_inline(text: str) -> str:
    """Escape text and translate inline `code` spans and bold runs to HTML."""
    parts = INLINE_CODE_RE.split(text)
    return "".join(f"<code>{esc(part)}</code>" if index % 2 else render_bold(part)
                   for index, part in enumerate(parts))


def render_fence(language: str, body: str) -> str:
    """Render a fenced code block as highlighted Telegram HTML."""
    language = re.sub(r"[^A-Za-z0-9+#-]", "", language)[:20]
    attribute = f' class="language-{language}"' if language else ""
    return f"<pre><code{attribute}>{esc(body.rstrip(chr(10)))}</code></pre>"


def format_answer(text: str) -> str:
    """Escape untrusted output, then render fences, inline code, and bold as HTML.

    A fence without a closing marker (truncated output) is closed at the end of
    the text so the reply still renders as code.
    """
    rendered: list[str] = []
    position = 0
    for match in FENCE_RE.finditer(text):
        rendered.append(render_inline(text[position:match.start()]))
        rendered.append(render_fence(match.group(1).strip(), match.group(2)))
        position = match.end()
    rendered.append(render_inline(text[position:]))
    return "".join(rendered)


def choice_options(text: str) -> list[tuple[str, str]]:
    """Only offer buttons for a final, contiguous numbered question block.

    Blank lines inside the block are tolerated; any other trailing line means
    the answer did not end in a question list.
    """
    options: list[tuple[str, str]] = []
    for line in reversed(text.rstrip().splitlines()):
        if not line.strip():
            if options:
                continue
            break
        match = CHOICE_RE.match(line)
        if not match:
            break
        options.append((match.group(1), match.group(2)))
    options.reverse()
    if not (2 <= len(options) <= 8):
        return []
    if [int(number) for number, _ in options] != list(range(1, len(options) + 1)):
        return []
    return options


def new_session(cwd: str) -> dict:
    return {
        "cwd": cwd,
        "prev_cwd": None,
        "model": None,  # None means use omp CLI default configured model
        "thinking": None,  # None means use omp's configured thinking level
        "omp_session_id": None,
        "proc": None,
        "stopped": False,
        "started_at": None,
        "busy": False,
        "task": None,  # human label of the tracked subprocess, for /status
        "generation": 0,
        "lock": asyncio.Lock(),
        "choice": None,
        "model_choice": None,
    }


USER_SESSIONS: dict[int, dict] = {}


def get_session(user_id: int) -> dict:
    if user_id not in USER_SESSIONS:
        start = DEFAULT_CWD if os.path.isdir(DEFAULT_CWD) else str(Path.home())
        USER_SESSIONS[user_id] = new_session(start)
    return USER_SESSIONS[user_id]


def clear_choices(sess: dict) -> None:
    """Drop every pending inline menu so stale buttons cannot act."""
    sess["choice"] = None
    sess["model_choice"] = None




def is_authorized(user_id: int) -> bool:
    return bool(ALLOWED_USER_ID and str(user_id) == ALLOWED_USER_ID.strip())


def allowed_update(update: Update) -> bool:
    return (update.effective_chat is not None and update.effective_chat.type == "private"
            and update.effective_user is not None and is_authorized(update.effective_user.id))


async def reject_busy(sess: dict, message) -> bool:
    if sess["busy"]:
        await message.reply_text("⏳ A task is already running. Use /stop first.")
        return True
    return False


def git_env() -> dict[str, str]:
    """Environment for every git subprocess: GitHub authority plus commit identity.

    Git resolves `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` pairs from the
    environment, so the configured authority reaches omp and every shell it
    spawns without writing the secret to disk. `GIT_TERMINAL_PROMPT=0` turns a
    missing credential into an immediate error instead of a prompt no bot can
    answer, and `BatchMode=yes` does the same for SSH passphrase prompts.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    entries: list[tuple[str, str]] = []
    if GIT_USER_NAME:
        entries.append(("user.name", GIT_USER_NAME))
    if GIT_USER_EMAIL:
        entries.append(("user.email", GIT_USER_EMAIL))
    if GITHUB_TOKEN:
        # The token takes precedence: rewrite SSH remotes to authenticated HTTPS.
        env["GITHUB_TOKEN"] = GITHUB_TOKEN
        env["GITHUB_USERNAME"] = GITHUB_USERNAME
        entries += [
            ("url.https://github.com/.insteadOf", "git@github.com:"),
            ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
            ("credential.https://github.com/.username", GITHUB_USERNAME),
            ("credential.https://github.com/.helper",
             '!f() { echo username="$GITHUB_USERNAME"; echo password="$GITHUB_TOKEN"; }; f'),
        ]
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


async def terminate(proc: asyncio.subprocess.Process) -> None:
    """Signal the whole process group, escalating to SIGKILL after a grace period."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
            return
        except asyncio.TimeoutError:
            continue


async def git(cwd: str, *args: str, timeout: float | None = None,
              track: dict | None = None) -> tuple[int, str, str]:
    """Run git in its own process group.

    `track` publishes the handle on the session so `/stop` can terminate a
    hanging push or pull; `timeout` bounds network operations that would
    otherwise keep the session busy forever.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(),
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "", "git executable not found"
    if track is not None:
        track["proc"] = proc
    try:
        if timeout is None:
            out, err = await proc.communicate()
        else:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                await terminate(proc)
                return -1, "", f"git {args[0]} timed out after {timeout:.0f}s"
    finally:
        if track is not None and track["proc"] is proc:
            track["proc"] = None
    return proc.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


async def current_branch(cwd: str) -> str | None:
    code, out, _ = await git(cwd, "branch", "--show-current")
    if code == 0 and out:
        return out
    return None

async def fetch_available_models() -> list[dict]:
    """Query omp models catalog via --json."""
    try:
        proc = await asyncio.create_subprocess_exec(
            OMP_BIN,
            "models",
            "--json",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode(errors="replace"))
            return data.get("models", [])
    except Exception as exc:
        logger.warning("Failed to fetch models: %s", exc)
    return []


def telegram_length(text: str) -> int:
    """Telegram measures message text in UTF-16 code units."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


async def send_long(message, text: str, parse_mode: str | None = None, reply_markup=None) -> None:
    """Split replies within Telegram's limit after HTML encoding."""
    text = clean_text(text)
    while text:
        low, high = 1, min(len(text), TG_LIMIT)
        while low < high:
            mid = (low + high + 1) // 2
            rendered = format_answer(text[:mid]) if parse_mode == ParseMode.HTML else text[:mid]
            if telegram_length(rendered) <= TG_LIMIT:
                low = mid
            else:
                high = mid - 1
        cut = low
        if cut < len(text):
            if parse_mode == ParseMode.HTML and text[:cut].count("```") % 2:
                fence_start = text.rfind("```", 0, cut)
                if fence_start > cut // 3:
                    cut = fence_start
            boundary = text.rfind("\n", 0, cut)
            if boundary > cut // 2:
                cut = boundary + 1
            if parse_mode == ParseMode.HTML and text[:cut].count("**") % 2:
                boundary = text.rfind("**", 0, cut)
                if boundary > 0:
                    cut = boundary
        part, text = text[:cut], text[cut:]
        await message.reply_text(
            format_answer(part) if parse_mode == ParseMode.HTML else part,
            parse_mode=parse_mode,
            reply_markup=reply_markup if not text else None,
        )

async def send_pre(message, heading: str, text: str) -> None:
    """Send command output in bounded escaped HTML preformatted chunks."""
    text = clean_text(text) or "(no output)"
    while text:
        low, high = 1, min(len(text), TG_LIMIT)
        while low < high:
            mid = (low + high + 1) // 2
            if telegram_length(heading + f"<pre>{esc(text[:mid])}</pre>") <= TG_LIMIT:
                low = mid
            else:
                high = mid - 1
        part, text = text[:low], text[low:]
        await message.reply_text(f"{heading}<pre>{esc(part)}</pre>", parse_mode=ParseMode.HTML)


async def keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


def tool_line(name: str, args: dict) -> str:
    """One-line summary of a tool invocation."""
    if not isinstance(args, dict):
        return f"🔧 {name}"
    detail = (
        args.get("command")
        or args.get("path")
        or args.get("pattern")
        or args.get("query")
        or args.get("url")
        or args.get("intent")
        or ""
    )
    detail = " ".join(str(detail).split())
    if len(detail) > 120:
        detail = detail[:117] + "..."
    return f"🔧 {name}: {detail}" if detail else f"🔧 {name}"


def extract_text(message: dict) -> str:
    parts = message.get("content") or []
    if not isinstance(parts, list):
        return ""
    texts = [part.get("text") for part in parts
             if isinstance(part, dict) and part.get("type") == "text"
             and isinstance(part.get("text"), str)]
    return clean_text("\n".join(texts))[-MAX_STREAM_TEXT:]


def final_text_from_events(events: list[dict]) -> str:
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "agent_end":
            continue
        messages = event.get("messages") or []
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = extract_text(message)
            if text:
                return text
    return ""


class StreamState:
    def __init__(self) -> None:
        self.tool_lines: list[str] = []
        self.thinking = ""
        self.text = ""
        self.session_id: str | None = None
        self.last_edit = 0.0
        self.events: list[dict] = []

    def render(self, elapsed: float) -> str:
        lines = [f"⏳ <b>Working</b> · {elapsed:.0f}s"]
        if self.tool_lines:
            latest = self.tool_lines[-1]
            lines.append(f"<code>{esc(latest)}</code>")
        return "\n".join(lines)


async def iter_json_lines(stream: asyncio.StreamReader):
    """Drain oversized lines without retaining them or parsing partial JSON."""
    buf = bytearray()
    oversized = False
    while chunk := await stream.read(65536):
        parts = chunk.split(b"\n")
        for segment in parts[:-1]:
            if not oversized and len(buf) + len(segment) <= MAX_EVENT_LINE:
                buf.extend(segment)
                if buf.strip():
                    yield buf.decode(errors="replace").strip()
            buf.clear()
            oversized = False
        segment = parts[-1]
        if not oversized and len(buf) + len(segment) <= MAX_EVENT_LINE:
            buf.extend(segment)
        else:
            buf.clear()
            oversized = True
    if buf and not oversized:
        yield buf.decode(errors="replace").strip()


async def pump_stdout(proc: asyncio.subprocess.Process, state: StreamState, on_update) -> None:
    """Parse omp's JSON event stream, updating Telegram at a throttled rate."""
    assert proc.stdout is not None
    async for line in iter_json_lines(proc.stdout):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        state.events.append(event)
        if len(state.events) > MAX_EVENTS:
            del state.events[:-MAX_EVENTS]
        kind = event.get("type")
        dirty = False

        if kind == "session":
            session_id = event.get("id")
            if isinstance(session_id, str):
                state.session_id = session_id
        elif kind == "message_start":
            message = event.get("message") or {}
            # tool_execution_start gives us the authoritative tool run events.
            if isinstance(message, dict) and message.get("role") == "assistant":
                for part in message.get("content") if isinstance(message.get("content"), list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "thinking":
                        state.thinking = str(part.get("thinking", "") or state.thinking)[-MAX_STREAM_TEXT:]
                        dirty = True
                    elif part.get("type") == "text":
                        state.text = str(part.get("text", "") or state.text)[-MAX_STREAM_TEXT:]
                        dirty = True
        elif kind == "message_update":
            inner = event.get("assistantMessageEvent") or {}
            inner_kind = inner.get("type") if isinstance(inner, dict) else None
            if inner_kind == "thinking_delta":
                state.thinking = (state.thinking + str(inner.get("delta", "")))[-MAX_STREAM_TEXT:]
                dirty = True
            elif inner_kind == "text_delta":
                state.text = (state.text + str(inner.get("delta", "")))[-MAX_STREAM_TEXT:]
                dirty = True
        elif kind == "tool_execution_start":
            state.tool_lines.append(tool_line(str(event.get("toolName", "tool"))[:80], event.get("args") or {}))
            del state.tool_lines[:-MAX_TOOL_LINES]
            dirty = True
        elif kind == "tool_execution_end":
            if event.get("isError"):
                state.tool_lines.append(f"⚠️ {str(event.get('toolName', 'tool'))[:80]} failed")
                del state.tool_lines[:-MAX_TOOL_LINES]
                dirty = True
        elif kind == "agent_end":
            state.agent_end = event
            dirty = True

        if not dirty:
            continue
        now = time.monotonic()
        if now - state.last_edit >= STATUS_EDIT_INTERVAL:
            state.last_edit = now
            await on_update()

async def run_omp(user_id: int, prompt: str, message, context: ContextTypes.DEFAULT_TYPE) -> None:
    sess = get_session(user_id)
    cwd = sess["cwd"]
    generation = sess["generation"]
    session_id = sess["omp_session_id"]
    model = sess["model"]
    thinking = sess["thinking"]
    chat_id = message.chat_id
    env = git_env()
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = f"{Path(OMP_BIN).parent}:{Path.home()}/.local/bin:{env['PATH']}"
    status = await message.reply_text("⏳ Working...")
    if sess["stopped"]:
        await status.edit_text("🛑 Task cancelled.")
        return
    state = StreamState()
    started = time.monotonic()
    sess["started_at"] = started
    sess["task"] = "prompt"
    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(context, chat_id, typing_stop))

    async def on_update() -> None:
        try:
            await status.edit_text(state.render(time.monotonic() - started), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    async def launch(resume: str | None, selected_model: str | None, selected_thinking: str | None):
        cmd = [OMP_BIN, "-p", "--auto-approve", "--mode", "json"]
        if selected_model:
            cmd += ["--model", selected_model]
        if selected_thinking:
            cmd += ["--thinking", selected_thinking]
        if resume:
            cmd += ["-r", resume]
        cmd.extend(["--", prompt])
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        sess["proc"] = proc
        if sess["stopped"]:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        async def drain_stderr() -> bytes:
            chunks = bytearray()
            while data := await proc.stderr.read(8192):
                remaining = 65536 - len(chunks)
                if remaining > 0:
                    chunks.extend(data[:remaining])
            return bytes(chunks)

        stderr_task = asyncio.create_task(drain_stderr())
        try:
            await pump_stdout(proc, state, on_update)
            stderr = await stderr_task
            await proc.wait()
            return proc, stderr
        finally:
            if proc.returncode is None:
                await terminate(proc)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            if sess["proc"] is proc:
                sess["proc"] = None

    proc = None
    stderr_bytes = b""
    try:
        proc, stderr_bytes = await launch(session_id, model, thinking)
        error = stderr_bytes.decode(errors="replace").lower()
        resume_rejected = bool(re.search(r"session\s+.+?\s+not found", error))
        # A nonzero exit is not sufficient evidence for replay: work may have
        # happened before failure. Retry only explicit pre-execution rejection.
        if (proc.returncode != 0 and not sess["stopped"] and not state.events
                and not state.session_id and session_id and resume_rejected):
            logger.warning("Session %s resume rejected; retrying fresh", session_id)
            state = StreamState()
            proc, stderr_bytes = await launch(None, model, thinking)
            error = stderr_bytes.decode(errors="replace").lower()
        model_rejected = (
            bool(re.search(r'model\s+(?:".+?"\s+)?not found', error))
            or any(word in error for word in (
                "unknown model", "invalid model", "model not found", "unsupported model",
                "model is not available", "unrecognized model",
            ))
        )
        if (proc.returncode != 0 and not sess["stopped"] and not state.events
                and not state.session_id and model != FALLBACK_MODEL and model_rejected):
            logger.warning("Model %s rejected; falling back to %s", model, FALLBACK_MODEL)
            state = StreamState()
            try:
                await status.edit_text(
                    f"⚠️ Model <code>{esc(model or 'default')}</code> unavailable. "
                    f"Falling back to <code>{esc(FALLBACK_MODEL)}</code>...",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            if not sess["stopped"]:
                proc, stderr_bytes = await launch(None, FALLBACK_MODEL, thinking)
                if proc.returncode == 0:
                    sess["model"] = FALLBACK_MODEL
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Failed to run omp")
        await status.edit_text(f"❌ Failed to run omp: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    finally:
        typing_stop.set()
        await typing_task
        sess["proc"] = None
        sess["started_at"] = None
        sess["task"] = None



    if generation == sess["generation"]:
        # Failed or interrupted runs must not restore stale session identities.
        sess["omp_session_id"] = state.session_id if proc.returncode == 0 and not sess["stopped"] else None
    answer = final_text_from_events(state.events) or clean_text(state.text)
    stderr_text = clean_text(stderr_bytes.decode(errors="replace"))
    try:
        await status.delete()
    except Exception:
        pass
    if answer:
        options = choice_options(answer)
        markup = None
        if (options and proc.returncode == 0 and not sess["stopped"]
                and generation == sess["generation"] and sess["omp_session_id"]):
            token = secrets.token_urlsafe(12)
            sess["choice"] = (token, chat_id, sess["omp_session_id"],
                              {number: label for number, label in options})
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{number}. {label[:55]}", callback_data=f"choice:{token}:{number}")]
                for number, label in options
            ])
        else:
            sess["choice"] = None
        await send_long(message, answer, ParseMode.HTML, markup)
    else:
        sess["choice"] = None
        if stderr_text:
            await send_long(message, f"⚠️ omp produced no output.\n\n{stderr_text}")
        else:
            await message.reply_text(f"⚠️ omp exited with code {proc.returncode} and no output.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    branch = await current_branch(sess["cwd"])
    lines = [
        "🤖 <b>OMP Telegram Bot</b>",
        "",
        f"📂 <b>Working dir:</b> <code>{esc(sess['cwd'])}</code>",
        f"🌿 <b>Branch:</b> <code>{esc(branch)}</code>" if branch else "🌿 <b>Branch:</b> <i>n/a (not a git repo)</i>",
        f"🧩 <b>Model:</b> <code>{esc(sess['model'] or 'default')}</code>",
        f"🧠 <b>Thinking:</b> <code>{esc(sess['thinking'] or 'default')}</code>",
        f"🗂 <b>Session:</b> <code>{esc(sess['omp_session_id'] or 'new')}</code>",
        "",
        "<b>Commands</b>",
        "/model [selector] — choose or reset the omp model",
        "/thinking [level] — show or set thinking level",
        "/cd &lt;path&gt; — change directory (use <code>/cd -</code> for previous)",
        "/pwd — current directory, branch, and status",
        "/diff [staged] — show working tree or staged diff",
        "/log [n] — show recent commits",
        "/branch — list local/remote branches",
        "/checkout &lt;branch&gt; — switch to an existing branch",
        "/pull [remote] [branch] — fast-forward pull from remote",
        "/push [remote] [branch] — push commits to remote",
        "/status — running task info",
        "/stop — abort the running task",
        "/reset — start a fresh omp session",
        "/help — this message",
        "",
        "Send any plain text to run it through <code>omp</code>.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    cwd = sess["cwd"]
    out = [f"📂 <code>{esc(cwd)}</code>"]

    code, _, err = await git(cwd, "rev-parse", "--is-inside-work-tree")
    if code == 0:
        branch = await current_branch(cwd)
        if branch:
            out.append(f"🌿 <b>Branch:</b> <code>{esc(branch)}</code>")
        else:
            _, head, _ = await git(cwd, "rev-parse", "--short", "HEAD")
            out.append(f"🌿 <b>Detached HEAD:</b> <code>{esc(head)}</code>")
        _, status, _ = await git(cwd, "status", "-sb")
        out.append("")
        out.append(f"<pre>{esc(status[:1200] or 'clean working tree')}</pre>")
    else:
        out.append("ℹ️ not a git repository")
        if err and "not a git repository" not in err.lower():
            out.append(f"<i>{esc(err)}</i>")

    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if context.args and await reject_busy(sess, update.message):
        return
    if not context.args:
        await update.message.reply_text(
            f"📂 <code>{esc(sess['cwd'])}</code>\n"
            "Usage: <code>/cd &lt;path&gt;</code> · <code>/cd -</code> returns to the previous directory",
            parse_mode=ParseMode.HTML,
        )
        return

    target = " ".join(context.args)
    if target == "-":
        if not sess["prev_cwd"]:
            await update.message.reply_text("ℹ️ No previous working directory recorded.")
            return
        resolved = Path(sess["prev_cwd"])
    elif target.startswith("~"):
        resolved = Path(os.path.expanduser(target))
    else:
        resolved = Path(target)
        if not resolved.is_absolute():
            resolved = Path(sess["cwd"]) / resolved

    resolved = resolved.expanduser().resolve()
    root = Path(WORKSPACE_ROOT).expanduser().resolve() if WORKSPACE_ROOT else None
    if root and not resolved.is_relative_to(root):
        await update.message.reply_text("❌ Path is outside the configured workspace.")
        return
    if not resolved.exists():
        await update.message.reply_text(
            f"❌ Path does not exist:\n<code>{esc(str(resolved))}</code>", parse_mode=ParseMode.HTML
        )
        return
    if not resolved.is_dir():
        await update.message.reply_text(
            f"❌ Not a directory:\n<code>{esc(str(resolved))}</code>", parse_mode=ParseMode.HTML
        )
        return

    previous = sess["cwd"]
    sess["cwd"] = str(resolved)
    if previous != sess["cwd"]:
        sess["prev_cwd"] = previous
    sess["omp_session_id"] = None  # sessions are scoped per working directory
    sess["generation"] += 1
    clear_choices(sess)

    branch = await current_branch(sess["cwd"])
    suffix = f"\n🌿 <b>Branch:</b> <code>{esc(branch)}</code>" if branch else "\nℹ️ not a git repository"
    await update.message.reply_text(
        f"✅ <b>Working dir:</b> <code>{esc(sess['cwd'])}</code>{suffix}\n🧠 omp session reset for this directory.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    code, _, _ = await git(sess["cwd"], "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await update.message.reply_text("❌ Not a git repository.")
        return
    code, out, err = await git(sess["cwd"], "branch", "-a", "--sort=-committerdate")
    if code != 0:
        await send_pre(update.message, f"❌ git error in <code>{esc(sess['cwd'])}</code>:\n", err or out)
        return
    lines = out.splitlines()[:60]
    await send_pre(update.message, f"🌿 <b>Branches</b> (<code>{esc(sess['cwd'])}</code>)\n", "\n".join(lines))


async def cmd_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/checkout &lt;existing-branch&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if await reject_busy(sess, update.message):
        return
    sess["busy"] = True
    sess["task"] = "checkout"
    try:
        await perform_checkout(sess, context.args, update.message)
    finally:
        sess["busy"] = False
        sess["task"] = None


async def perform_checkout(sess: dict, args: list[str], message) -> None:
    code, _, _ = await git(sess["cwd"], "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await message.reply_text("❌ Not a git repository.")
        return

    if len(args) != 1 or args[0].startswith("-"):
        await message.reply_text("Usage: /checkout <existing-branch>")
        return
    branch = args[0]
    valid, _, _ = await git(sess["cwd"], "check-ref-format", "--branch", branch)
    if valid != 0:
        await message.reply_text("❌ Invalid branch name.")
        return
    code, out, err = await git(sess["cwd"], "switch", "--no-guess", "--", branch,
                               track=sess, timeout=GIT_TIMEOUT)
    if code != 0:
        await send_pre(message, "❌ Checkout failed:\n", err or out)
        return
    branch = await current_branch(sess["cwd"])
    sess["omp_session_id"] = None
    sess["generation"] += 1
    clear_choices(sess)
    detail = clean_text("\n".join(x for x in (out, err) if x))
    await send_pre(message, f"✅ <b>Branch:</b> <code>{esc(branch or '?')}</code>\n", detail)


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if await reject_busy(sess, update.message):
        return
    sess["busy"] = True
    try:
        await perform_push(sess, context.args or [], update.message)
    finally:
        sess["busy"] = False


async def perform_push(sess: dict, args: list[str], message) -> None:
    cwd = sess["cwd"]
    code, _, _ = await git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await message.reply_text("❌ Not a git repository.")
        return

    flags: list[str] = []
    targets: list[str] = []
    for arg in args:
        if arg in PUSH_FLAGS:
            flags.append(arg)
        elif not arg.startswith("-") and PUSH_REF_RE.match(arg):
            targets.append(arg)
        else:
            await message.reply_text(
                f"❌ Unsupported argument: <code>{esc(arg)}</code>\n"
                "Usage: <code>/push [remote] [branch] [-u|--tags|--dry-run|--force-with-lease]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    if targets:
        cmd = ["push", *flags, *targets]
    else:
        branch = await current_branch(cwd)
        if not branch:
            await message.reply_text(
                "❌ Detached HEAD. Name the destination explicitly, e.g. <code>/push origin main</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        upstream_code, _, _ = await git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        cmd = ["push", *flags, *([] if upstream_code == 0 else ["-u", "origin", branch])]

    status = await message.reply_text("⏳ Pushing...")
    sess["task"] = "push"
    try:
        code, out, err = await git(cwd, *cmd, track=sess, timeout=GIT_TIMEOUT)
    finally:
        sess["task"] = None
    detail = "\n".join(x for x in (out, err) if x) or "(no output)"
    try:
        await status.delete()
    except Exception:
        pass
    await send_pre(message, "✅ <b>Push complete</b>\n" if code == 0 else "❌ <b>Push failed</b>\n", detail)


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if await reject_busy(sess, update.message):
        return
    await perform_diff(sess, context.args or [], update.message)


async def perform_diff(sess: dict, args: list[str], message) -> None:
    cwd = sess["cwd"]
    code, _, _ = await git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await message.reply_text("❌ Not a git repository.")
        return

    staged = False
    if args:
        first = args[0].lower()
        if first in {"staged", "--staged", "--cached"}:
            staged = True
        else:
            await message.reply_text("Usage: <code>/diff [staged]</code>", parse_mode=ParseMode.HTML)
            return

    diff_target = ["--cached"] if staged else []
    _, stat_out, _ = await git(cwd, "diff", "--stat", *diff_target)
    diff_code, diff_out, err = await git(cwd, "diff", *diff_target)
    if diff_code != 0:
        await send_pre(message, "❌ <b>Diff error</b>\n", err or diff_out)
        return

    if not diff_out and not stat_out:
        label = "staged changes" if staged else "unstaged changes"
        await message.reply_text(f"ℹ️ No {label}.")
        return

    body = f"{stat_out}\n\n{diff_out}".strip()
    if len(body) > DIFF_LIMIT:
        body = body[:DIFF_LIMIT] + "\n... (truncated)"
    heading = f"📝 <b>Diff ({'staged' if staged else 'working tree'})</b>\n"
    await send_pre(message, heading, body)


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if await reject_busy(sess, update.message):
        return
    cwd = sess["cwd"]
    code, _, _ = await git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await update.message.reply_text("❌ Not a git repository.")
        return

    count = LOG_DEFAULT
    if context.args:
        try:
            count = int(context.args[0])
            if count < 1:
                count = LOG_DEFAULT
            elif count > LOG_MAX:
                count = LOG_MAX
        except ValueError:
            await update.message.reply_text(
                f"Usage: <code>/log [1-{LOG_MAX}]</code>", parse_mode=ParseMode.HTML
            )
            return

    code, out, err = await git(cwd, "log", "--oneline", f"-n{count}")
    if code != 0:
        await send_pre(update.message, "❌ <b>Log error</b>\n", err or out)
        return
    await send_pre(update.message, f"📜 <b>Git Log (last {count})</b>\n", out or "(no commits)")

PULL_FLAGS = {"--ff-only", "--rebase", "--no-rebase"}


async def cmd_pull(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if await reject_busy(sess, update.message):
        return
    sess["busy"] = True
    try:
        await perform_pull(sess, context.args or [], update.message)
    finally:
        sess["busy"] = False


async def perform_pull(sess: dict, args: list[str], message) -> None:
    cwd = sess["cwd"]
    code, _, _ = await git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await message.reply_text("❌ Not a git repository.")
        return

    flags: list[str] = []
    targets: list[str] = []
    for arg in args:
        if arg in PULL_FLAGS:
            flags.append(arg)
        elif not arg.startswith("-") and PUSH_REF_RE.match(arg):
            targets.append(arg)
        else:
            await message.reply_text(
                f"❌ Unsupported argument: <code>{esc(arg)}</code>\n"
                "Usage: <code>/pull [remote] [branch] [--ff-only|--rebase|--no-rebase]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    if not any(f in flags for f in ("--ff-only", "--rebase", "--no-rebase")):
        flags.append("--ff-only")

    status = await message.reply_text("⏳ Pulling...")
    sess["task"] = "pull"
    try:
        code, out, err = await git(cwd, "pull", *flags, *targets, track=sess, timeout=GIT_TIMEOUT)
    finally:
        sess["task"] = None
    detail = "\n".join(x for x in (out, err) if x) or "(no output)"
    try:
        await status.delete()
    except Exception:
        pass
    await send_pre(message, "✅ <b>Pull complete</b>\n" if code == 0 else "❌ <b>Pull failed</b>\n", detail)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    proc = sess["proc"]
    if sess["busy"] or (proc is not None and proc.returncode is None):
        elapsed = time.monotonic() - (sess["started_at"] or time.monotonic())
        await update.message.reply_text(
            f"⏳ <b>{esc(sess['task'] or 'task')}</b> running for {elapsed:.0f}s in "
            f"<code>{esc(sess['cwd'])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.message.reply_text(
        f"💤 Idle.\n📂 <code>{esc(sess['cwd'])}</code>\n"
        f"🧩 <b>Model:</b> <code>{esc(sess['model'] or 'default')}</code>\n"
        f"🧠 <b>Thinking:</b> <code>{esc(sess['thinking'] or 'default')}</code>\n"
        f"🗂 <b>Session:</b> <code>{esc(sess['omp_session_id'] or 'new')}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    proc = sess["proc"]
    if not sess["busy"] and (proc is None or proc.returncode is not None):
        await update.message.reply_text("ℹ️ No task is running.")
        return
    sess["stopped"] = True
    sess["generation"] += 1
    sess["omp_session_id"] = None
    clear_choices(sess)
    if proc is None or proc.returncode is not None:
        await update.message.reply_text("🛑 Task cancelled.")
        return
    try:
        await terminate(proc)
        await update.message.reply_text("🛑 Task terminated.")
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Stop failed: {esc(str(exc))}", parse_mode=ParseMode.HTML)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if await reject_busy(sess, update.message):
        return
    sess["omp_session_id"] = None
    sess["generation"] += 1
    clear_choices(sess)
    await update.message.reply_text("🔄 Session cleared. Next message starts a fresh omp session.")

def apply_model(sess: dict, model: str | None) -> None:
    """Switch the active selector and invalidate the session bound to the old one."""
    sess["model"] = model
    sess["omp_session_id"] = None
    sess["generation"] += 1
    clear_choices(sess)


def model_set_text(sess: dict) -> str:
    return (
        f"✅ <b>Model set:</b> <code>{esc(sess['model'])}</code>\n"
        f"↩️ Falls back to <code>{esc(FALLBACK_MODEL)}</code> if it fails.\n"
        "🧠 Session cleared so the new model starts clean."
    )


def model_menu(sess: dict, chat_id: int, selectors: list[str]):
    """First page of the inline model picker, or None when the catalog is empty."""
    if not selectors:
        return None
    page = selectors[:MODEL_PAGE_SIZE]
    token = secrets.token_urlsafe(12)
    sess["model_choice"] = (token, chat_id, {str(index): selector for index, selector in enumerate(page)})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(selector[:55], callback_data=f"model:{token}:{index}")]
        for index, selector in enumerate(page)
    ])


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if context.args and await reject_busy(sess, update.message):
        return

    if not context.args:
        models = await fetch_available_models()
        selectors = [m["selector"] for m in models if m.get("selector")]
        listing = "\n".join(f"• <code>{esc(selector)}</code>" for selector in selectors[:40])
        await update.message.reply_text(
            f"🧩 <b>Model:</b> <code>{esc(sess['model'] or 'default')}</code>\n"
            f"↩️ Fallback on failure: <code>{esc(FALLBACK_MODEL)}</code>\n\n"
            f"<b>Available</b>\n{listing or '<i>catalog unavailable</i>'}\n\n"
            "Usage: <code>/model &lt;selector&gt;</code> · <code>/model default</code> to reset",
            parse_mode=ParseMode.HTML,
            reply_markup=model_menu(sess, update.message.chat_id, selectors),
        )
        return

    choice = " ".join(context.args).strip()
    if choice.lower() in {"default", "reset", "clear", "auto"}:
        apply_model(sess, None)
        await update.message.reply_text(
            "✅ Model reset to omp default; session cleared.", parse_mode=ParseMode.HTML
        )
        return

    models = await fetch_available_models()
    if await reject_busy(sess, update.message):
        return
    selectors = [m["selector"] for m in models if m.get("selector")]
    match = next((s for s in selectors if s.lower() == choice.lower()), None)
    if match is None:
        match = next((s for s in selectors if choice.lower() in s.lower()), None)

    if selectors and match is None:
        await update.message.reply_text(
            f"❌ Unknown model <code>{esc(choice)}</code>.\nUse <code>/model</code> to list options.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Catalog unavailable: still let omp attempt the value (it does fuzzy matching itself).
    apply_model(sess, match or choice)
    await update.message.reply_text(model_set_text(sess), parse_mode=ParseMode.HTML)


async def cmd_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    levels = " · ".join(f"<code>{level}</code>" for level in THINKING_LEVELS)
    if not context.args:
        await update.message.reply_text(
            f"🧠 <b>Thinking level:</b> <code>{esc(sess['thinking'] or 'default')}</code>\n"
            f"Levels: {levels}\n"
            "Usage: <code>/thinking &lt;level&gt;</code> · <code>/thinking default</code> to reset",
            parse_mode=ParseMode.HTML,
        )
        return
    if await reject_busy(sess, update.message):
        return

    choice = context.args[0].strip().lower()
    if choice in {"default", "reset", "clear", "none"}:
        sess["thinking"] = None
    elif choice in THINKING_LEVELS:
        sess["thinking"] = choice
    else:
        await update.message.reply_text(
            f"❌ Unknown thinking level <code>{esc(choice)}</code>.\nLevels: {levels}",
            parse_mode=ParseMode.HTML,
        )
        return

    sess["omp_session_id"] = None
    sess["generation"] += 1
    clear_choices(sess)
    await update.message.reply_text(
        f"✅ <b>Thinking level:</b> <code>{esc(sess['thinking'] or 'default')}</code>\n"
        "🧠 Session cleared so the new level starts clean.",
        parse_mode=ParseMode.HTML,
    )



async def on_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not allowed_update(update):
        return

    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    sess = get_session(user_id)
    if sess["busy"]:
        await update.message.reply_text("⏳ A task is already running. Use /stop first.")
        return
    sess["busy"] = True
    sess["stopped"] = False
    clear_choices(sess)
    try:
        async with sess["lock"]:
            await run_omp(user_id, prompt, update.message, context)
    except Exception:
        logger.exception("omp run failed")
        await update.message.reply_text("❌ Internal error while running omp. Check service logs.")
    finally:
        sess["busy"] = False


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    if not allowed_update(update):
        await query.answer("Unauthorized", show_alert=True)
        return
    sess = get_session(user_id)
    choice = sess.get("choice")
    parts = (query.data or "").split(":")
    if (len(parts) != 3 or not choice or parts[1] != choice[0]
            or query.message.chat_id != choice[1] or sess["omp_session_id"] != choice[2]
            or parts[2] not in choice[3]):
        await query.answer("This choice has expired.", show_alert=True)
        return
    if sess["busy"]:
        await query.answer("A task is still running.", show_alert=True)
        return
    sess["busy"] = True
    sess["stopped"] = False
    clear_choices(sess)
    await query.answer()
    try:
        async with sess["lock"]:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ Selected {parts[2]}. {choice[3][parts[2]]}")
            await run_omp(user_id, parts[2], query.message, context)
    except Exception:
        logger.exception("omp choice run failed")
        await query.message.reply_text("❌ Internal error while running omp. Check service logs.")
    finally:
        sess["busy"] = False

async def on_model_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not allowed_update(update):
        await query.answer("Unauthorized", show_alert=True)
        return
    sess = get_session(update.effective_user.id)
    menu = sess.get("model_choice")
    parts = (query.data or "").split(":")
    if (len(parts) != 3 or not menu or parts[1] != menu[0]
            or query.message.chat_id != menu[1] or parts[2] not in menu[2]):
        await query.answer("This menu has expired.", show_alert=True)
        return
    if sess["busy"]:
        await query.answer("A task is still running.", show_alert=True)
        return
    selector = menu[2][parts[2]]
    sess["model_choice"] = None
    apply_model(sess, selector)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(model_set_text(sess), parse_mode=ParseMode.HTML)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Welcome, current dir and session info"),
            BotCommand("cd", "Change working directory"),
            BotCommand("pwd", "Current directory, branch, git status"),
            BotCommand("branch", "List git branches"),
            BotCommand("checkout", "Switch to an existing git branch"),
            BotCommand("diff", "Show working tree or staged diff"),
            BotCommand("log", "Show recent commits"),
            BotCommand("pull", "Pull commits from the remote"),
            BotCommand("push", "Push commits to the remote"),
            BotCommand("model", "Show or switch the omp model"),
            BotCommand("thinking", "Show or set the omp thinking level"),
            BotCommand("status", "Show running task"),
            BotCommand("stop", "Abort running task"),
            BotCommand("reset", "Start a fresh omp session"),
            BotCommand("help", "Usage instructions"),
        ]
    )
    logger.info("Bot commands registered.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        return

    logger.info("Starting OMP Telegram Bot (omp=%s, github_authority=%s)",
                OMP_BIN, "token" if GITHUB_TOKEN else "ssh/host")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("pwd", cmd_pwd))
    app.add_handler(CommandHandler("branch", cmd_branch))
    app.add_handler(CommandHandler("checkout", cmd_checkout))
    app.add_handler(CommandHandler("diff", cmd_diff))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("pull", cmd_pull))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("thinking", cmd_thinking))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^choice:"))
    app.add_handler(CallbackQueryHandler(on_model_choice, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
