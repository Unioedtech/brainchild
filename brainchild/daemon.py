"""Main daemon event loop.

One Python process. Polls TG. Runs scheduled jobs. Invokes claude on inbound
messages. Owner-only allowlist. /pause /resume /status handled inline.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import subprocess

from brainchild import claude_runner, scheduler as sched_mod, tg, vault
from brainchild.config import PATHS, Config, is_paused
from brainchild.security import (
    assert_secrets_perms, audit, audit_rotate, get_secret, pause, resume,
    write_settings_daemon,
)

log = logging.getLogger("brainchild.daemon")
_shutdown = threading.Event()


def _setup_logging() -> None:
    PATHS.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        PATHS.logs_dir / "daemon.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Also echo to stdout when run in foreground
    if sys.stdout.isatty() or os.environ.get("BRAINCHILD_FOREGROUND"):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(sh)


def _install_signals() -> None:
    def handler(signum, _frame):
        log.info("signal=%s status=shutdown", signum)
        _shutdown.set()
    for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        s = getattr(signal, sig_name, None)
        if s is not None:
            try:
                signal.signal(s, handler)
            except (ValueError, OSError):
                pass


def _sweep_tmp() -> None:
    """Boot-time: delete files in tmp/ older than 1h."""
    if not PATHS.tmp_dir.exists():
        return
    cutoff = time.time() - 3600
    for f in PATHS.tmp_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


# ---- inbound message handler -------------------------------------------------

def _handle_message(client: tg.TGClient, cfg: Config, msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    log.info("msg in chat_id=%s text=%r voice=%s doc=%s photo=%s",
             chat_id, text[:80], "voice" in msg, "document" in msg, "photo" in msg)

    if cfg.owner_chat_id is not None and chat_id != cfg.owner_chat_id:
        log.warning("ignored msg: owner=%s actual=%s — set owner_chat_id correctly in config.toml",
                    cfg.owner_chat_id, chat_id)
        return

    # Inline commands (no LLM)
    if text == "/pause":
        pause()
        client.send(chat_id, "Paused. Send /resume to start again.")
        return
    if text == "/resume":
        resume()
        client.send(chat_id, "Resumed.")
        return
    if text == "/status":
        client.send(chat_id, _status_text(cfg))
        return
    if text == "/start":
        client.send(chat_id, "Brainchild is alive. Talk to me.")
        return

    if is_paused():
        client.send(chat_id, "I'm paused (/resume to start).")
        return

    # Voice
    if "voice" in msg:
        text = _handle_voice(client, cfg, msg)
        if not text:
            return

    # Documents / photos → enqueue to inbox, ack
    if "document" in msg or "photo" in msg:
        _ingest_attachment(client, cfg, msg)
        return

    if not text:
        return

    snapshot = vault.read_safe(PATHS.snapshot)
    if not snapshot:
        log.warning("no snapshot yet — building one synchronously")
        try:
            vault.write_snapshot(cfg.vault_path)
            snapshot = vault.read_safe(PATHS.snapshot)
        except Exception:
            log.exception("snapshot build failed")
    prompt = claude_runner.compose_prompt(snapshot, text)
    log.info("invoking claude prompt_chars=%d model=%s timeout=%ds",
             len(prompt), cfg.claude_model, cfg.claude_timeout_sec)
    started = time.time()
    try:
        with client.typing(chat_id):
            reply = claude_runner.run(prompt, cfg, trigger="tg")
    except claude_runner.ClaudeAuthExpired:
        log.error("claude auth expired during message handle")
        client.send(chat_id, "Claude session expired on the host laptop. Run `claude` then /login there.")
        return
    except subprocess.TimeoutExpired:
        log.error("claude timed out after %ds", cfg.claude_timeout_sec)
        client.send(chat_id, f"Claude took longer than {cfg.claude_timeout_sec}s — likely rate-limited on your tier. Try again in a minute.")
        return
    except Exception as e:
        log.exception("claude run failed")
        client.send(chat_id, f"Something broke: {type(e).__name__}: {e}")
        return
    log.info("claude reply_chars=%d duration=%ds", len(reply), int(time.time() - started))

    dur = int(time.time() - started)
    if reply.strip():
        client.send(chat_id, tg.markdown_to_html(reply.strip()))
    else:
        client.send(chat_id, "(no reply)")

    # Queue LOG entry — backfill cron picks it up
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    vault.queue_log_entry(f"### {ts} [tg]\n**you:** {text[:200]}\n**me:** {reply.strip()[:400]}\n_(took {dur}s)_")


def _handle_voice(client: tg.TGClient, cfg: Config, msg: dict) -> str:
    if not cfg.voice_enabled:
        client.send(msg["chat"]["id"], "Voice is off. Enable with `brainchild config voice on`.")
        return ""
    try:
        from brainchild import voice as voice_mod
    except ImportError:
        client.send(msg["chat"]["id"], "Voice deps not installed. See README.")
        return ""
    file_id = msg["voice"]["file_id"]
    try:
        ogg = client.download_file(file_id, PATHS.tmp_dir)
        text = voice_mod.transcribe(ogg, cfg=cfg)
        ogg.unlink(missing_ok=True)
        if not text.strip():
            client.send(msg["chat"]["id"], "Couldn't hear that — try again?")
            return ""
        client.send(msg["chat"]["id"], f"<i>heard:</i> {text}")
        return text
    except Exception as e:
        log.exception("voice failed")
        client.send(msg["chat"]["id"], f"Voice transcription broke: {e}")
        return ""


def _ingest_attachment(client: tg.TGClient, cfg: Config, msg: dict) -> None:
    file_id = None
    if "document" in msg:
        file_id = msg["document"]["file_id"]
    elif "photo" in msg:
        # take largest photo
        photos = msg["photo"]
        file_id = max(photos, key=lambda p: p.get("file_size", 0))["file_id"]
    if not file_id:
        return
    inbox = cfg.vault_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        dest = client.download_file(file_id, inbox)
        client.send(msg["chat"]["id"], f"Saved to inbox: <code>{dest.name}</code>")
    except Exception as e:
        client.send(msg["chat"]["id"], f"Couldn't save attachment: {e}")


def _status_text(cfg: Config) -> str:
    paused = "PAUSED" if is_paused() else "running"
    return (
        f"<b>brainchild: {paused}</b>\n"
        f"vault: <code>{cfg.vault_path}</code>\n"
        f"model: <code>{cfg.claude_model}</code>\n"
        f"voice: {'on' if cfg.voice_enabled else 'off'}"
    )


# ---- scheduled job bodies ----------------------------------------------------

def _job_snapshot(cfg: Config) -> None:
    vault.write_snapshot(cfg.vault_path)


def _job_log_backfill(cfg: Config) -> None:
    vault.drain_log_inbox(cfg.vault_path)


def _job_audit_rotate(_cfg: Config) -> None:
    audit_rotate()


def _job_briefing(client: tg.TGClient, cfg: Config, window: str) -> None:
    if cfg.owner_chat_id is None:
        return
    snapshot = vault.read_safe(PATHS.snapshot)
    prompt = claude_runner.compose_prompt(
        snapshot,
        f"It's the {window} briefing window. Read the briefing recipe for {window} "
        f"in agent/briefing-recipes.md and produce that briefing now. Be tight, no warmup.",
    )
    try:
        out = claude_runner.run(prompt, cfg, trigger=f"briefing-{window}")
    except Exception:
        log.exception("briefing %s failed", window)
        return
    if out.strip():
        client.send(cfg.owner_chat_id, tg.markdown_to_html(out.strip()))


def _job_day_rollover(client: tg.TGClient, cfg: Config) -> None:
    if cfg.owner_chat_id is None:
        return
    snapshot = vault.read_safe(PATHS.snapshot)
    prompt = claude_runner.compose_prompt(
        snapshot,
        "It's 02:00 — day rollover. Read agent/LIVE.md, archive the Today section "
        "to sessions/<yesterday>.md, regenerate Today with 3 priorities for the new "
        "day based on PERSONA, LIVE, SCOREBOARD, push-back-rules, and recent LOG. "
        "Write directly. When done, reply with a one-line summary.",
    )
    try:
        out = claude_runner.run(prompt, cfg, trigger="day-rollover")
    except Exception:
        log.exception("day rollover failed")
        return
    if out.strip():
        client.send(cfg.owner_chat_id, "<b>rollover:</b>\n" + tg.markdown_to_html(out.strip()))


# ---- main loop ---------------------------------------------------------------

def run() -> None:
    _setup_logging()
    _install_signals()
    log.info("brainchild boot pid=%s", os.getpid())

    PATHS.ensure()
    assert_secrets_perms()
    write_settings_daemon()
    _sweep_tmp()

    cfg = Config.load(PATHS.config_file)
    vault.ensure_vault(cfg.vault_path)
    vault.assert_safe(cfg.vault_path)
    # First snapshot synchronously so the first message has state
    try:
        vault.write_snapshot(cfg.vault_path)
    except Exception:
        log.exception("initial snapshot failed (ok if vault is fresh)")

    token = get_secret("telegram_token")
    if not token:
        log.error("no telegram_token in keyring/secrets — run `brainchild install`")
        return
    client = tg.TGClient(token, owner_chat_id=cfg.owner_chat_id)

    sched = sched_mod.Scheduler()
    sched.add(sched_mod.Job("snapshot", lambda: _job_snapshot(cfg), interval_sec=cfg.snapshot_interval_sec))
    sched.add(sched_mod.Job("log_backfill", lambda: _job_log_backfill(cfg), interval_sec=cfg.log_backfill_interval_sec))
    sched.add(sched_mod.Job("audit_rotate", lambda: _job_audit_rotate(cfg), interval_sec=3600 * 24))
    if cfg.briefing_am:
        sched.add(sched_mod.Job("briefing_am", lambda: _job_briefing(client, cfg, "AM"), daily_time=cfg.briefing_am))
    if cfg.briefing_midday:
        sched.add(sched_mod.Job("briefing_midday", lambda: _job_briefing(client, cfg, "Midday"), daily_time=cfg.briefing_midday))
    if cfg.briefing_precraft:
        sched.add(sched_mod.Job("briefing_precraft", lambda: _job_briefing(client, cfg, "Pre-craft"), daily_time=cfg.briefing_precraft))
    if cfg.briefing_night:
        sched.add(sched_mod.Job("briefing_night", lambda: _job_briefing(client, cfg, "Night"), daily_time=cfg.briefing_night))
    if cfg.day_rollover:
        sched.add(sched_mod.Job("day_rollover", lambda: _job_day_rollover(client, cfg), daily_time=cfg.day_rollover))

    # Send hello if first boot since install
    boot_marker = PATHS.state_dir / "booted_once"
    if not boot_marker.exists() and cfg.owner_chat_id:
        try:
            client.send(cfg.owner_chat_id, "<b>brainchild is alive</b> — reading your context now.")
            boot_marker.touch()
        except Exception:
            log.exception("hello send failed")

    log.info("loop start jobs=%d owner_chat_id=%s", len(sched.jobs), cfg.owner_chat_id)
    while not _shutdown.is_set():
        if is_paused():
            _shutdown.wait(2)
            continue
        try:
            sched.tick()
            updates = client.poll()
            if updates:
                log.info("poll returned %d updates", len(updates))
            for u in updates:
                msg = u.get("message")
                if msg:
                    _handle_message(client, cfg, msg)
        except Exception:
            log.exception("loop iteration error")
            _shutdown.wait(5)

    log.info("brainchild shutdown clean")
