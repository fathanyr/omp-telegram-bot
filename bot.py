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

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CHOICE_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

def format_answer(text: str) -> str:
    """Escape untrusted output, then translate paired Markdown bold to Telegram HTML."""
    parts = BOLD_RE.split(text)
    return "".join(f"<b>{esc(part)}</b>" if index % 2 else esc(part)
                   for index, part in enumerate(parts))


def choice_options(text: str) -> list[tuple[str, str]]:
    """Only offer buttons for a final, contiguous numbered question block."""
    lines = text.rstrip().splitlines()
    options: list[tuple[str, str]] = []
    for line in reversed(lines):
        match = CHOICE_RE.match(line)
        if match:
            options.append((match.group(1), match.group(2)))
        elif options:
            break
    options.reverse()
    if not (2 <= len(options) <= 8):
        return []
    if [int(number) for number, _ in options] != list(range(1, len(options) + 1)):
        return []
    return options


def clean_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").strip()


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def new_session(cwd: str) -> dict:
    return {
        "cwd": cwd,
        "model": None,  # None means use omp CLI default configured model
        "omp_session_id": None,
        "proc": None,
        "stopped": False,
        "started_at": None,
        "busy": False,
        "generation": 0,
        "lock": asyncio.Lock(),
        "choice": None,
    }


USER_SESSIONS: dict[int, dict] = {}


def get_session(user_id: int) -> dict:
    if user_id not in USER_SESSIONS:
        start = DEFAULT_CWD if os.path.isdir(DEFAULT_CWD) else str(Path.home())
        USER_SESSIONS[user_id] = new_session(start)
    return USER_SESSIONS[user_id]


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


async def git(cwd: str, *args: str) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "git executable not found"
    out, err = await proc.communicate()
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
            lines.append("🔧 Using tools" + (" · ⚠️ one failed" if any("failed" in line for line in self.tool_lines) else ""))
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
    chat_id = message.chat_id
    env = os.environ.copy()
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = f"{Path(OMP_BIN).parent}:{Path.home()}/.local/bin:{env['PATH']}"
    status = await message.reply_text("⏳ Working...")
    if sess["stopped"]:
        await status.edit_text("🛑 Task cancelled.")
        return
    state = StreamState()
    started = time.monotonic()
    sess["started_at"] = started
    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(context, chat_id, typing_stop))

    async def on_update() -> None:
        try:
            await status.edit_text(state.render(time.monotonic() - started), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    async def launch(resume: str | None, selected_model: str | None):
        cmd = [OMP_BIN, "-p", "--auto-approve", "--mode", "json"]
        if selected_model:
            cmd += ["--model", selected_model]
        if resume:
            cmd += ["-r", resume]
        cmd.append(prompt)
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
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            if sess["proc"] is proc:
                sess["proc"] = None

    proc = None
    stderr_bytes = b""
    try:
        proc, stderr_bytes = await launch(session_id, model)
        error = stderr_bytes.decode(errors="replace").lower()
        resume_rejected = bool(re.search(r"session\s+.+?\s+not found", error))
        # A nonzero exit is not sufficient evidence for replay: work may have
        # happened before failure. Retry only explicit pre-execution rejection.
        if (proc.returncode != 0 and not sess["stopped"] and not state.events
                and not state.session_id and session_id and resume_rejected):
            logger.warning("Session %s resume rejected; retrying fresh", session_id)
            state = StreamState()
            proc, stderr_bytes = await launch(None, model)
            error = stderr_bytes.decode(errors="replace").lower()
        model_rejected = any(word in error for word in (
            "unknown model", "invalid model", "model not found", "unsupported model",
            "model is not available", "unrecognized model",
        ))
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
                proc, stderr_bytes = await launch(None, FALLBACK_MODEL)
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
        f"🧠 <b>Session:</b> <code>{esc(sess['omp_session_id'] or 'new')}</code>",
        f"🧩 <b>Model:</b> <code>{esc(sess.get('model') or 'default')}</code>",
        "",
        "<b>Commands</b>",
        "/model — show or switch the omp model",
        "/cd &lt;path&gt; — change working directory",
        "/pwd — current dir, branch, git status",
        "/branch — list local/remote branches",
        "/checkout &lt;branch&gt; — switch to an existing branch",
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
            f"📂 <code>{esc(sess['cwd'])}</code>\nUsage: <code>/cd &lt;path&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    target = " ".join(context.args)
    if target.startswith("~"):
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

    sess["cwd"] = str(resolved.resolve())
    sess["omp_session_id"] = None  # sessions are scoped per working directory
    sess["generation"] += 1
    sess["choice"] = None

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
    try:
        await perform_checkout(sess, context.args, update.message)
    finally:
        sess["busy"] = False


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
    code, out, err = await git(sess["cwd"], "switch", "--no-guess", "--", branch)
    if code != 0:
        await send_pre(message, "❌ Checkout failed:\n", err or out)
        return
    branch = await current_branch(sess["cwd"])
    sess["omp_session_id"] = None
    sess["generation"] += 1
    sess["choice"] = None
    detail = clean_text("\n".join(x for x in (out, err) if x))
    await send_pre(message, f"✅ <b>Branch:</b> <code>{esc(branch or '?')}</code>\n", detail)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    proc = sess["proc"]
    if proc and proc.returncode is None:
        elapsed = time.monotonic() - (sess["started_at"] or time.monotonic())
        await update.message.reply_text(
            f"⏳ omp running for {elapsed:.0f}s in <code>{esc(sess['cwd'])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.message.reply_text(
        f"💤 Idle.\n📂 <code>{esc(sess['cwd'])}</code>\n"
        f"🧠 session <code>{esc(sess['omp_session_id'] or 'new')}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    proc = sess["proc"]
    if not sess["busy"]:
        await update.message.reply_text("ℹ️ No task is running.")
        return
    sess["stopped"] = True
    sess["generation"] += 1
    sess["omp_session_id"] = None
    sess["choice"] = None
    if proc is None or proc.returncode is not None:
        await update.message.reply_text("🛑 Task cancelled.")
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        await update.message.reply_text("🛑 Task terminated.")
    except ProcessLookupError:
        await update.message.reply_text("ℹ️ Process already exited.")
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
    sess["choice"] = None
    await update.message.reply_text("🔄 Session cleared. Next message starts a fresh omp session.")

async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed_update(update):
        return
    sess = get_session(update.effective_user.id)
    if context.args and await reject_busy(sess, update.message):
        return

    if not context.args:
        current = sess.get("model") or "default"
        models = await fetch_available_models()
        selectors = [m["selector"] for m in models if m.get("selector")]
        listing = "\n".join(f"• <code>{esc(s)}</code>" for s in selectors[:40])
        await update.message.reply_text(
            f"🧩 <b>Model:</b> <code>{esc(current)}</code>\n"
            f"↩️ Fallback on failure: <code>{esc(FALLBACK_MODEL)}</code>\n\n"
            f"<b>Available</b>\n{listing or '<i>catalog unavailable</i>'}\n\n"
            "Usage: <code>/model &lt;selector&gt;</code> · <code>/model default</code> to reset",
            parse_mode=ParseMode.HTML,
        )
        return

    choice = " ".join(context.args).strip()
    if choice.lower() in {"default", "reset", "clear", "auto"}:
        sess["model"] = None
        sess["omp_session_id"] = None
        sess["choice"] = None
        sess["generation"] += 1
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
    sess["model"] = match or choice
    sess["omp_session_id"] = None  # model switch invalidates session context
    sess["generation"] += 1
    sess["choice"] = None
    await update.message.reply_text(
        f"✅ <b>Model set:</b> <code>{esc(sess['model'])}</code>\n"
        f"↩️ Falls back to <code>{esc(FALLBACK_MODEL)}</code> if it fails.\n"
        "🧠 Session cleared so the new model starts clean.",
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
    sess["choice"] = None
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
    sess["choice"] = None
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

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Welcome, current dir and session info"),
            BotCommand("cd", "Change working directory"),
            BotCommand("pwd", "Current directory, branch, git status"),
            BotCommand("branch", "List git branches"),
            BotCommand("checkout", "Switch to an existing git branch"),
            BotCommand("model", "Show or switch the omp model"),
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

    logger.info("Starting OMP Telegram Bot (omp=%s)", OMP_BIN)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("pwd", cmd_pwd))
    app.add_handler(CommandHandler("branch", cmd_branch))
    app.add_handler(CommandHandler("checkout", cmd_checkout))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^choice:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
