"""Vault reader/writer and snapshot generator.

The vault is plain markdown on disk. The daemon reads it for the snapshot
that gets injected into every claude prompt.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from brainchild.config import PATHS, Config

log = logging.getLogger("brainchild.vault")

AGENT_FILES = (
    "PERSONA.md", "LIVE.md", "BACKLOG.md", "SCOREBOARD.md",
    "LOG.md", "briefing-recipes.md", "push-back-rules.md",
)

# Soft cap for the snapshot — keep it lean so per-message prompts stay fast
SNAPSHOT_MAX_CHARS = 6000


def ensure_vault(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    for sub in ("agent", "streams", "inbox", "sessions"):
        (vault / sub).mkdir(exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def read_safe(path: Path, default: str = "") -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return default


def assert_safe(vault: Path) -> None:
    """Refuse to use vault if it's a symlink or outside $HOME."""
    home = Path.home().resolve()
    p = vault.resolve()
    if vault.is_symlink():
        raise RuntimeError(f"vault path is a symlink: {vault}")
    try:
        p.relative_to(home)
    except ValueError:
        raise RuntimeError(f"vault path is outside $HOME: {vault}")


def append_log(vault: Path, entry: str) -> None:
    """Append-only LOG.md write. Newest on top."""
    log_file = vault / "agent" / "LOG.md"
    existing = read_safe(log_file)
    if not existing:
        existing = "---\ntype: log\n---\n\n# Log\n<!-- append-only, newest on top -->\n"
    # Insert after the header sentinel
    marker = "<!-- append-only, newest on top -->\n"
    if marker in existing:
        head, tail = existing.split(marker, 1)
        new = head + marker + "\n" + entry.rstrip() + "\n" + tail
    else:
        new = existing.rstrip() + "\n\n" + entry.rstrip() + "\n"
    atomic_write(log_file, new)


def build_snapshot(vault: Path, log_tail_lines: int = 30) -> str:
    """Curate a ≤6KB snapshot for claude prompt injection."""
    parts: list[str] = []
    parts.append(f"# Brainchild snapshot — generated {datetime.now().isoformat(timespec='seconds')}\n")

    persona = read_safe(vault / "agent" / "PERSONA.md")
    if persona:
        parts.append("## PERSONA\n" + _trim(persona, 1800))

    live = read_safe(vault / "agent" / "LIVE.md")
    if live:
        parts.append("## LIVE\n" + _trim(live, 1400))

    scoreboard = read_safe(vault / "agent" / "SCOREBOARD.md")
    if scoreboard:
        parts.append("## SCOREBOARD\n" + _trim(scoreboard, 800))

    push_back = read_safe(vault / "agent" / "push-back-rules.md")
    if push_back:
        parts.append("## PUSH-BACK\n" + _trim(push_back, 600))

    log_file = read_safe(vault / "agent" / "LOG.md")
    if log_file:
        tail = "\n".join(log_file.splitlines()[:log_tail_lines + 6])
        parts.append("## LOG (recent)\n" + _trim(tail, 1200))

    snap = "\n\n".join(parts)
    if len(snap) > SNAPSHOT_MAX_CHARS:
        snap = snap[:SNAPSHOT_MAX_CHARS] + "\n…(truncated)"
    return snap


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…(truncated)"


def write_snapshot(vault: Path) -> Path:
    snap = build_snapshot(vault)
    atomic_write(PATHS.snapshot, snap)
    return PATHS.snapshot


# ---- pending log inbox (async backfill from daemon to LOG.md) ----------------

def queue_log_entry(entry: str) -> None:
    """Append to the pending log inbox; backfill cron drains into LOG.md."""
    PATHS.pending_log.parent.mkdir(parents=True, exist_ok=True)
    with PATHS.pending_log.open("a") as f:
        f.write(entry.rstrip() + "\n\n")


def drain_log_inbox(vault: Path) -> int:
    """Move queued log entries into LOG.md. Returns chars drained."""
    if not PATHS.pending_log.exists():
        return 0
    try:
        content = PATHS.pending_log.read_text()
    except OSError:
        return 0
    if not content.strip():
        return 0
    append_log(vault, content)
    PATHS.pending_log.write_text("")
    return len(content)
