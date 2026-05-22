"""Interactive Telegram bot pairing for the install wizard.

Walks the user through @BotFather, validates the token, polls getUpdates
for the first /start to discover chat_id.
"""
from __future__ import annotations

import sys
import time

from brainchild import tg

BOTFATHER_INSTRUCTIONS = """\
[Telegram setup]

  1. Open Telegram and search for @BotFather:
        https://t.me/BotFather
  2. Send: /newbot
  3. Pick any display name (e.g. "Aman Brainchild")
  4. Pick a username ending in 'bot' (e.g. amanbrain_bot)
  5. BotFather will reply with an HTTP API token. Paste it below.
"""


def pair_telegram() -> tuple[str, int]:
    """Returns (token, chat_id) or raises on cancel."""
    print(BOTFATHER_INSTRUCTIONS)
    while True:
        token = input("  Token: ").strip()
        if not token:
            again = input("  Empty token. Try again? [Y/n] ").strip().lower()
            if again in ("n", "no"):
                raise KeyboardInterrupt("install cancelled")
            continue
        client = tg.TGClient(token)
        info = client.get_me()
        if not info.get("ok"):
            print(f"  ✗ token didn't work: {info.get('description', 'unknown error')}")
            print("  Check for stray spaces and paste again.")
            continue
        bot_name = info["result"]["username"]
        print(f"  ✓ paired with @{bot_name}")
        break

    print("\nNow message your bot once so it learns your chat ID:")
    print(f"  1. Find @{bot_name} in Telegram (search the username)")
    print("  2. Tap Start, or send /start")
    print("\n  Waiting for your first message... (5 min timeout)")
    sys.stdout.flush()

    deadline = time.time() + 300
    last_print = 0
    while time.time() < deadline:
        chat_id = _quick_poll(client)
        if chat_id:
            print(f"\r  ✓ paired with chat_id {chat_id}".ljust(60))
            try:
                client.send(chat_id, "Brainchild paired. Installing now…")
            except Exception:
                pass
            return token, chat_id
        now = time.time()
        if now - last_print > 2:
            remaining = int(deadline - now)
            mins, secs = divmod(remaining, 60)
            sys.stdout.write(f"\r  Waiting... ({mins}:{secs:02d} remaining)  ")
            sys.stdout.flush()
            last_print = now
        time.sleep(1)
    print("\n  ✗ timed out waiting for /start. Re-run install to retry.")
    raise TimeoutError("chat_id discovery timed out")


def _quick_poll(client: tg.TGClient) -> int | None:
    try:
        resp = client._get("getUpdates", timeout=2)
    except Exception:
        return None
    for u in resp.get("result", []):
        msg = u.get("message") or {}
        if msg.get("text") == "/start" or msg.get("text"):
            return msg["chat"]["id"]
    return None
