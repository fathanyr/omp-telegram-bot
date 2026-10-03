#!/usr/bin/env python3
"""Operator notification helper: send one Telegram message to the configured chat.

Invoked only through `docker exec` by the host operator. It opens no socket,
accepts no commands, and reads its credentials solely from the container
environment (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_ID`), so a caller never handles
a token or a chat id.

Usage:
    python notify.py "<message>"
    printf '%s' "<message>" | python notify.py -

Exit status: 0 when Telegram accepted the message; non-zero otherwise.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from telegram import Bot
from telegram.error import TelegramError

MAX_MESSAGE_CHARS = 4000
EXIT_USAGE = 2
EXIT_MESSAGE = 3
EXIT_CREDENTIALS = 4
EXIT_SEND = 5

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Keep newlines and tabs; drop the remaining C0/C1 controls Telegram rejects.
CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F-\x9F]")
USAGE = 'usage: notify.py "<message>" or notify.py - (message on stdin)'


def sanitize(text: str) -> str:
    """Strip ANSI escapes and control characters, then trim the result."""
    return CONTROL_RE.sub("", ANSI_RE.sub("", text)).replace("\r\n", "\n").strip()


def validate(raw: str) -> tuple[bool, str]:
    """Return (accepted, sanitized message or failure reason)."""
    message = sanitize(raw)
    if not message:
        return False, "message is empty or whitespace-only"
    if len(message) > MAX_MESSAGE_CHARS:
        return False, f"message is {len(message)} characters; limit is {MAX_MESSAGE_CHARS}"
    return True, message


def read_message(args: list[str], stream) -> tuple[bool, str]:
    if len(args) != 1:
        return False, USAGE
    raw = stream.read() if args[0] == "-" else args[0]
    return validate(raw)


def redact(text: str, secrets: list[str]) -> str:
    """Remove credential values from text that may reach the terminal."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def credentials() -> tuple[bool, str, int | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is not set in the environment", None
    raw_chat = os.environ.get("ALLOWED_USER_ID", "").strip()
    if not raw_chat:
        return False, "ALLOWED_USER_ID is not set in the environment", None
    if not raw_chat.isdigit():
        return False, "ALLOWED_USER_ID is not a decimal chat id", None
    return True, token, int(raw_chat)


async def deliver(token: str, chat_id: int, message: str) -> None:
    """Send exactly one message through python-telegram-bot."""
    async with Bot(token=token) as bot:
        await bot.send_message(chat_id=chat_id, text=message)


def main(argv: list[str]) -> int:
    accepted, payload = read_message(argv[1:], sys.stdin)
    if not accepted:
        reason = payload
        if payload == USAGE:
            print(f"notification failed: {reason}", file=sys.stderr)
            return EXIT_USAGE
        print(f"notification failed: {reason}", file=sys.stderr)
        return EXIT_MESSAGE

    ok, token, chat_id = credentials()
    if not ok:
        print(f"notification failed: {token}", file=sys.stderr)
        return EXIT_CREDENTIALS
    assert chat_id is not None

    try:
        asyncio.run(deliver(token, chat_id, payload))
    except TelegramError as exc:
        reason = redact(sanitize(str(exc.message or type(exc).__name__)), [token, str(chat_id)])
        print(f"notification failed: {type(exc).__name__}: {reason}", file=sys.stderr)
        return EXIT_SEND
    except Exception as exc:  # network, DNS, TLS, asyncio teardown
        reason = redact(sanitize(str(exc)), [token, str(chat_id)]) or type(exc).__name__
        print(f"notification failed: {reason}", file=sys.stderr)
        return EXIT_SEND

    print("notification sent")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
