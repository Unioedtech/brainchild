"""`brainchild` command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from brainchild import daemon, security, tg
from brainchild.config import PATHS, Config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brainchild")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("daemon", help="run daemon in foreground (default)")
    sub.add_parser("install", help="run the install wizard")
    sub.add_parser("status", help="show daemon state")
    sub.add_parser("pause", help="pause processing")
    sub.add_parser("resume", help="resume processing")
    ask = sub.add_parser("ask", help="one-shot prompt to your bot")
    ask.add_argument("text", nargs="+")
    audit = sub.add_parser("audit", help="inspect audit log")
    audit_sub = audit.add_subparsers(dest="audit_cmd")
    tail = audit_sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=50)
    since = audit_sub.add_parser("since")
    since.add_argument("duration", help="e.g. 1h, 30m, 2d")
    rotate = sub.add_parser("rotate-token", help="rotate Telegram bot token")
    sub.add_parser("uninstall", help="remove brainchild")
    cfg_cmd = sub.add_parser("config", help="show or set config values")
    cfg_cmd.add_argument("key", nargs="?")
    cfg_cmd.add_argument("value", nargs="?")

    args = p.parse_args(argv)
    cmd = args.cmd or "daemon"

    if cmd == "daemon":
        os.environ.setdefault("BRAINCHILD_FOREGROUND", "1")
        daemon.run()
        return 0
    if cmd == "install":
        from brainchild.install import run_wizard
        run_wizard()
        return 0
    if cmd == "status":
        return _status()
    if cmd == "pause":
        security.pause()
        print("paused. /resume or brainchild resume to start again.")
        return 0
    if cmd == "resume":
        security.resume()
        print("resumed.")
        return 0
    if cmd == "ask":
        return _ask(" ".join(args.text))
    if cmd == "audit":
        return _audit(args)
    if cmd == "rotate-token":
        return _rotate_token()
    if cmd == "uninstall":
        return _uninstall()
    if cmd == "config":
        return _config(args)
    p.print_help()
    return 1


def _status() -> int:
    cfg = Config.load(PATHS.config_file)
    print(f"paused:   {security.is_paused()}")
    print(f"vault:    {cfg.vault_path}")
    print(f"model:    {cfg.claude_model}")
    print(f"voice:    {'on' if cfg.voice_enabled else 'off'}")
    print(f"chat_id:  {cfg.owner_chat_id}")
    tail = security.audit_tail(3)
    if tail:
        print("\nlast audit:")
        for evt in tail:
            ts = datetime.fromtimestamp(evt["ts"]).strftime("%H:%M:%S")
            print(f"  {ts} {evt.get('tool')} {evt.get('trigger')} exit={evt.get('exit_code')}")
    return 0


def _ask(text: str) -> int:
    cfg = Config.load(PATHS.config_file)
    token = security.get_secret("telegram_token")
    if not token or not cfg.owner_chat_id:
        print("not installed. run `brainchild install`.")
        return 1
    client = tg.TGClient(token, owner_chat_id=cfg.owner_chat_id)
    client.send(cfg.owner_chat_id, tg.markdown_to_html(text))
    print("sent.")
    return 0


def _audit(args) -> int:
    sub = args.audit_cmd
    if sub == "tail":
        for evt in security.audit_tail(args.n):
            ts = datetime.fromtimestamp(evt["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{ts} {json.dumps({k: v for k, v in evt.items() if k != 'ts'})}")
        return 0
    if sub == "since":
        seconds = _parse_duration(args.duration)
        for evt in security.audit_since(seconds):
            ts = datetime.fromtimestamp(evt["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{ts} {json.dumps({k: v for k, v in evt.items() if k != 'ts'})}")
        return 0
    print("usage: brainchild audit {tail [-n N] | since DURATION}")
    return 1


def _parse_duration(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    return int(s)


def _rotate_token() -> int:
    print("Token rotation:")
    print("  1. In Telegram, message @BotFather → /revoke → pick your bot")
    print("  2. Copy the new token, paste below.")
    new = input("New token: ").strip()
    if not new:
        print("aborted.")
        return 1
    client = tg.TGClient(new)
    info = client.get_me()
    if not info.get("ok"):
        print(f"token didn't work: {info.get('description')}")
        return 1
    security.set_secret("telegram_token", new)
    client.ack_all()
    print("✓ token rotated.")
    return 0


def _uninstall() -> int:
    cfg = Config.load(PATHS.config_file)
    print("This will:")
    print(f"  - stop the daemon and remove service registration")
    print(f"  - remove ~/.brainchild/")
    print(f"  ? delete vault at {cfg.vault_path}  (your synthesized content)")
    print(f"  ? delete secrets (Telegram token)")
    ans = input("\nContinue? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("aborted.")
        return 0
    # stop daemon + deregister service
    try:
        from brainchild.install import service_register
        service_register.unregister()
        print("  ✓ service unregistered")
    except Exception as e:
        print(f"  ! service unregister: {e}")

    if input("Delete secrets (Telegram token)? [Y/n] ").strip().lower() not in ("n", "no"):
        security.delete_secret("telegram_token")
        print("  ✓ secrets removed")

    delete_vault = input(f"Delete vault at {cfg.vault_path}? [y/N] ").strip().lower() in ("y", "yes")
    if delete_vault:
        import shutil
        if cfg.vault_path.exists():
            shutil.rmtree(cfg.vault_path)
            print(f"  ✓ vault deleted")

    delete_install = input("Delete ~/.brainchild/? [Y/n] ").strip().lower() not in ("n", "no")
    if delete_install:
        import shutil
        if PATHS.install_dir.exists():
            shutil.rmtree(PATHS.install_dir)
            print(f"  ✓ ~/.brainchild/ removed")

    print("\nDone.")
    return 0


def _config(args) -> int:
    if not args.key:
        print(PATHS.config_file.read_text() if PATHS.config_file.exists() else "(no config)")
        return 0
    print("(edit ~/.brainchild/config.toml directly; structured config writes coming in v0.2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
