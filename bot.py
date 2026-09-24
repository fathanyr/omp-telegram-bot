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
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
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

TG_LIMIT = 4000
STATUS_EDIT_INTERVAL = 1.5
MAX_TOOL_LINES = 8
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "openai-codex/gpt-5.6-luna")

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


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
        "lock": asyncio.Lock(),
    }


USER_SESSIONS: dict[int, dict] = {}


def get_session(user_id: int) -> dict:
    if user_id not in USER_SESSIONS:
        start = DEFAULT_CWD if os.path.isdir(DEFAULT_CWD) else str(Path.home())
        USER_SESSIONS[user_id] = new_session(start)
    return USER_SESSIONS[user_id]


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(user_id) == str(ALLOWED_USER_ID)


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


async def send_long(message, text: str, parse_mode: str | None = None) -> None:
    """Send text, splitting on line boundaries to stay under Telegram's limit."""
    text = clean_text(text)
    if not text:
        return
    while text:
        if len(text) <= TG_LIMIT:
            chunk, text = text, ""
        else:
            cut = text.rfind("\n", 0, TG_LIMIT)
            if cut < TG_LIMIT // 2:
                cut = TG_LIMIT
            chunk, text = text[:cut], text[cut:]
        await message.reply_text(chunk, parse_mode=parse_mode)


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
    texts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return clean_text("\n".join(t for t in texts if t))


def final_text_from_events(events: list[dict]) -> str:
    for event in reversed(events):
        if event.get("type") != "agent_end":
            continue
        messages = event.get("messages") or []
        for message in reversed(messages):
            if message.get("role") != "assistant":
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
        lines = [f"⏳ <b>Running omp</b> ({elapsed:.0f}s)"]
        if self.thinking:
            snippet = " ".join(self.thinking.split())
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            lines.append(f"🧠 <i>{esc(snippet)}</i>")
        if self.tool_lines:
            lines.append("")
            lines.extend(esc(line) for line in self.tool_lines[-MAX_TOOL_LINES:])
        if self.text:
            lines.append("")
            lines.append(esc(self.text[-400:]))
        return "\n".join(lines)[:TG_LIMIT]


async def iter_json_lines(stream: asyncio.StreamReader):
    """Yield complete lines without limit issues from StreamReader.readline()."""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            if buf:
                yield buf.decode(errors="replace").strip()
            break
        buf.extend(chunk)
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                break
            line = buf[:nl].decode(errors="replace").strip()
            del buf[: nl + 1]
            if line:
                yield line


async def pump_stdout(proc: asyncio.subprocess.Process, state: StreamState, on_update) -> None:
    """Parse omp's JSON event stream, updating Telegram at a throttled rate."""
    assert proc.stdout is not None
    async for line in iter_json_lines(proc.stdout):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        state.events.append(event)
        kind = event.get("type")
        dirty = False

        if kind == "session":
            state.session_id = event.get("id")
        elif kind == "message_start":
            message = event.get("message") or {}
            # Assistant message start: only capture thinking/text here.
            # tool_execution_start gives us the authoritative tool run events.
            if message.get("role") == "assistant":
                for part in message.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "thinking":
                        state.thinking = part.get("thinking", "") or state.thinking
                        dirty = True
                    elif part.get("type") == "text":
                        state.text = part.get("text", "") or state.text
                        dirty = True
        elif kind == "message_update":
            inner = event.get("assistantMessageEvent") or {}
            inner_kind = inner.get("type")
            if inner_kind == "thinking_delta":
                state.thinking += inner.get("delta", "")
                dirty = True
            elif inner_kind == "text_delta":
                state.text += inner.get("delta", "")
                dirty = True
        elif kind == "tool_execution_start":
            state.tool_lines.append(
                tool_line(event.get("toolName", "tool"), event.get("args") or {})
            )
            dirty = True
        elif kind == "tool_execution_end":
            if event.get("isError"):
                state.tool_lines.append(f"⚠️ {event.get('toolName', 'tool')} failed")
                dirty = True

        if not dirty:
            continue
        now = time.monotonic()
        if now - state.last_edit >= STATUS_EDIT_INTERVAL:
            state.last_edit = now
            await on_update()


async def run_omp(user_id: int, prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sess = get_session(user_id)
    chat_id = update.effective_chat.id
    cwd = sess["cwd"]


    env = os.environ.copy()
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = f"{Path(OMP_BIN).parent}:{Path.home()}/.local/bin:{env['PATH']}"

    status = await update.message.reply_text("⏳ Starting omp...")
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

    async def launch(session_id: str | None, model: str | None):
        nonlocal state
        cmd = [OMP_BIN, "-p", "--auto-approve", "--mode", "json"]
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["-r", session_id]
        cmd.append(prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        sess["proc"] = proc
        stderr_task = asyncio.create_task(proc.stderr.read())
        await pump_stdout(proc, state, on_update)
        err_bytes = await stderr_task
        await proc.wait()
        return proc, err_bytes

    used_model = sess.get("model")
    fallback_used = False
    try:
        sess["stopped"] = False
        proc, stderr_bytes = await launch(sess["omp_session_id"], used_model)

        if not sess["stopped"]:
            # Case A: resume failed immediately with no events -> retry with a fresh session
            if proc.returncode != 0 and sess["omp_session_id"] and not state.events:
                logger.warning("Session %s resume failed, retrying fresh", sess["omp_session_id"])
                sess["omp_session_id"] = None
                state = StreamState()
                proc, stderr_bytes = await launch(None, used_model)

        # Case B: run failed and we are not already on the fallback model -> fail over.
        # An explicit /stop must never trigger a fallback retry.
        if proc.returncode != 0 and used_model != FALLBACK_MODEL and not sess["stopped"]:
            logger.warning(
                "Model %s failed (exit %s); falling back to %s",
                used_model,
                proc.returncode,
                FALLBACK_MODEL,
            )
            fallback_used = True
            state = StreamState()
            sess["omp_session_id"] = None
            try:
                await status.edit_text(
                    f"⚠️ Model <code>{esc(used_model or 'default')}</code> failed. "
                    f"Falling back to <code>{esc(FALLBACK_MODEL)}</code>...",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            proc, stderr_bytes = await launch(None, FALLBACK_MODEL)
            used_model = FALLBACK_MODEL
    except Exception as exc:
        typing_stop.set()
        await typing_task
        await status.edit_text(f"❌ Failed to run omp: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    finally:
        typing_stop.set()
        await typing_task
        sess["proc"] = None
        sess["started_at"] = None

    elapsed = time.monotonic() - started
    if state.session_id:
        sess["omp_session_id"] = state.session_id

    answer = final_text_from_events(state.events) or clean_text(state.text)
    stderr_text = clean_text(stderr_bytes.decode(errors="replace"))
    try:
        await status.delete()
    except Exception:
        pass

    model_label = used_model or "default"
    fallback_note = " (fallback)" if fallback_used else ""
    header = (
        f"📂 <code>{esc(cwd)}</code> · 🧩 <code>{esc(model_label)}</code>{fallback_note}"
        f" · ⏱ {elapsed:.0f}s · exit {proc.returncode}"
    )
    await update.message.reply_text(header, parse_mode=ParseMode.HTML)

    if answer:
        await send_long(update.message, answer)
    elif stderr_text:
        await send_long(update.message, f"⚠️ omp produced no output.\n\n{stderr_text}")
    else:
        await update.message.reply_text(f"⚠️ omp exited with code {proc.returncode} and no output.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
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
        "/checkout &lt;branch&gt; — switch or create branch",
        "/status — running task info",
        "/stop — abort the running task",
        "/reset — start a fresh omp session",
        "/help — this message",
        "",
        "Send any plain text to run it through <code>omp</code>.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
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
    if not is_authorized(update.effective_user.id):
        return
    sess = get_session(update.effective_user.id)
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

    resolved = resolved.expanduser()
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

    branch = await current_branch(sess["cwd"])
    suffix = f"\n🌿 <b>Branch:</b> <code>{esc(branch)}</code>" if branch else "\nℹ️ not a git repository"
    await update.message.reply_text(
        f"✅ <b>Working dir:</b> <code>{esc(sess['cwd'])}</code>{suffix}\n🧠 omp session reset for this directory.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    sess = get_session(update.effective_user.id)
    code, out, err = await git(sess["cwd"], "branch", "-a", "--sort=-committerdate")
    if code != 0:
        await update.message.reply_text(
            f"❌ git error in <code>{esc(sess['cwd'])}</code>:\n<pre>{esc(err or out)}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = out.splitlines()[:60]
    await update.message.reply_text(
        f"🌿 <b>Branches</b> (<code>{esc(sess['cwd'])}</code>)\n<pre>{esc(chr(10).join(lines))}</pre>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    sess = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/checkout &lt;branch&gt;</code> or <code>/checkout -b &lt;new-branch&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    code, _, _ = await git(sess["cwd"], "rev-parse", "--is-inside-work-tree")
    if code != 0:
        await update.message.reply_text("❌ Not a git repository.", parse_mode=ParseMode.HTML)
        return

    args = list(context.args)
    if args[0] != "-b":
        await git(sess["cwd"], "fetch", "--all", "--prune")

    code, out, err = await git(sess["cwd"], "checkout", *args)
    if code != 0:
        await update.message.reply_text(
            f"❌ Checkout failed:\n<pre>{esc(err or out)}</pre>", parse_mode=ParseMode.HTML
        )
        return

    branch = await current_branch(sess["cwd"])
    sess["omp_session_id"] = None
    detail = clean_text("\n".join(x for x in (out, err) if x))
    await update.message.reply_text(
        f"✅ <b>Branch:</b> <code>{esc(branch or '?')}</code>\n<pre>{esc(detail[:600])}</pre>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
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
    if not is_authorized(update.effective_user.id):
        return
    sess = get_session(update.effective_user.id)
    proc = sess["proc"]
    if not proc or proc.returncode is not None:
        await update.message.reply_text("ℹ️ No task is running.")
        return
    try:
        sess["stopped"] = True
        pgid = os.getpgid(proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await asyncio.sleep(0.7)
        if proc.returncode is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Double-check any remaining children in the process group
        try:
            p = await asyncio.create_subprocess_exec(
                "pkill", "-9", "-g", str(pgid),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            await p.wait()
        except Exception:
            pass
        await update.message.reply_text("🛑 Task terminated.")
    except ProcessLookupError:
        await update.message.reply_text("ℹ️ Process already exited.")
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Stop failed: {esc(str(exc))}", parse_mode=ParseMode.HTML)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    sess = get_session(update.effective_user.id)
    sess["omp_session_id"] = None
    await update.message.reply_text("🔄 Session cleared. Next message starts a fresh omp session.")

async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    sess = get_session(update.effective_user.id)

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
        await update.message.reply_text(
            "✅ Model reset to omp default.", parse_mode=ParseMode.HTML
        )
        return

    models = await fetch_available_models()
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
    await update.message.reply_text(
        f"✅ <b>Model set:</b> <code>{esc(sess['model'])}</code>\n"
        f"↩️ Falls back to <code>{esc(FALLBACK_MODEL)}</code> if it fails.\n"
        "🧠 Session cleared so the new model starts clean.",
        parse_mode=ParseMode.HTML,
    )



async def on_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    sess = get_session(user_id)
    if sess["lock"].locked():
        await update.message.reply_text("⏳ A task is already running. Use /stop first.")
        return

    async with sess["lock"]:
        try:
            await run_omp(user_id, prompt, update, context)
        except Exception:
            logger.exception("omp run failed")
            await update.message.reply_text("❌ Internal error while running omp. Check service logs.")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Welcome, current dir and session info"),
            BotCommand("cd", "Change working directory"),
            BotCommand("pwd", "Current directory, branch, git status"),
            BotCommand("branch", "List git branches"),
            BotCommand("checkout", "Switch or create a git branch"),
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
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("pwd", cmd_pwd))
    app.add_handler(CommandHandler("branch", cmd_branch))
    app.add_handler(CommandHandler("checkout", cmd_checkout))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
