"""Security primitives: secret storage, audit log, killswitch, deny list.

Keyring with file fallback. Audit log is append-only JSONL with daily rotation.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from brainchild.config import PATHS

SERVICE = "brainchild"


# ---- Secret storage ----------------------------------------------------------

def _keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except Exception:
        return False


def set_secret(name: str, value: str) -> None:
    """Store a secret. Prefer OS keychain; fall back to 0600 JSON file."""
    if _keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE, name, value)
            return
        except Exception:
            pass
    _file_set(name, value)


def get_secret(name: str) -> str | None:
    if _keyring_available():
        try:
            import keyring
            v = keyring.get_password(SERVICE, name)
            if v is not None:
                return v
        except Exception:
            pass
    return _file_get(name)


def delete_secret(name: str) -> None:
    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE, name)
        except Exception:
            pass
    f = PATHS.secrets_dir / f"{name}.json"
    if f.exists():
        f.unlink()


def _file_set(name: str, value: str) -> None:
    PATHS.secrets_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(PATHS.secrets_dir, 0o700)
    f = PATHS.secrets_dir / f"{name}.json"
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"value": value}), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, f)


def _file_get(name: str) -> str | None:
    f = PATHS.secrets_dir / f"{name}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text()).get("value")
    except Exception:
        return None


def assert_secrets_perms() -> None:
    """Refuse to start if secrets dir is world-readable."""
    if sys.platform == "win32":
        return
    if not PATHS.secrets_dir.exists():
        return
    mode = PATHS.secrets_dir.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(
            f"~/.brainchild/secrets/ has mode {oct(mode)} — refuse to start. "
            f"chmod 700 ~/.brainchild/secrets/"
        )


# ---- Audit log ---------------------------------------------------------------

def audit(event: dict[str, Any]) -> None:
    """Append one JSON line to today's audit file. Never raises."""
    try:
        PATHS.audit_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        f = PATHS.audit_dir / f"{day}.jsonl"
        line = json.dumps({"ts": time.time(), **event}, default=str) + "\n"
        with f.open("a") as fh:
            fh.write(line)
        if sys.platform != "win32":
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
    except Exception:
        pass


def audit_rotate(keep_days: int = 90, gzip_after_days: int = 7) -> None:
    """Compress audit files older than gzip_after_days, delete >keep_days."""
    if not PATHS.audit_dir.exists():
        return
    now = datetime.now()
    for f in PATHS.audit_dir.iterdir():
        if not f.is_file():
            continue
        try:
            stem = f.stem.replace(".jsonl", "")
            d = datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        age = (now - d).days
        if age > keep_days:
            f.unlink()
        elif age > gzip_after_days and f.suffix == ".jsonl":
            gz = f.with_suffix(".jsonl.gz")
            with f.open("rb") as r, gzip.open(gz, "wb") as w:
                w.write(r.read())
            f.unlink()


def audit_tail(n: int = 50) -> list[dict]:
    day = datetime.now().strftime("%Y-%m-%d")
    f = PATHS.audit_dir / f"{day}.jsonl"
    if not f.exists():
        return []
    lines = f.read_text().splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def audit_since(seconds_ago: int) -> list[dict]:
    cutoff = time.time() - seconds_ago
    out: list[dict] = []
    now = datetime.now()
    for delta in (0, 1):
        day = (now - timedelta(days=delta)).strftime("%Y-%m-%d")
        f = PATHS.audit_dir / f"{day}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                evt = json.loads(line)
                if evt.get("ts", 0) >= cutoff:
                    out.append(evt)
            except json.JSONDecodeError:
                continue
    return out


# ---- Killswitch --------------------------------------------------------------

def pause() -> None:
    PATHS.pause_file.parent.mkdir(parents=True, exist_ok=True)
    PATHS.pause_file.touch()


def resume() -> None:
    if PATHS.pause_file.exists():
        PATHS.pause_file.unlink()


def is_paused() -> bool:
    return PATHS.pause_file.exists()


# ---- Default Claude deny list ------------------------------------------------

DEFAULT_DENY = [
    "Bash(rm:*)",
    "Bash(rm -rf*)",
    "Bash(rm -fr*)",
    "Bash(sudo:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(chmod:*)",
    "Bash(chown:*)",
    "Bash(chmod 777*)",
    "Bash(chmod -R 777*)",
    "Bash(launchctl:*)",
    "Bash(systemctl:*)",
    "Bash(crontab:*)",
    "Bash(dd:*)",
    "Bash(nc:*)",
    "Bash(ncat:*)",
    "Bash(mkfs:*)",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(killall *)",
    "Bash(diskutil erase*)",
    "Bash(security delete*)",
    "Edit(/etc/**)",
    "Edit(/System/**)",
    "Edit(~/.ssh/**)",
    "Edit(~/.aws/**)",
    "Edit(~/.gcp/**)",
    "Edit(~/.gnupg/**)",
    "Edit(~/.brainchild/secrets/**)",
    "Edit(~/Library/LaunchAgents/**)",
    "Edit(~/Library/Keychains/**)",
    "Edit(~/.zshrc)",
    "Edit(~/.bashrc)",
    "Edit(~/.zprofile)",
    "Edit(~/.bash_profile)",
    "Write(/etc/**)",
    "Write(/System/**)",
    "Write(~/.ssh/**)",
    "Write(~/.aws/**)",
    "Write(~/.brainchild/secrets/**)",
    "WebFetch",
    "WebSearch",
]


def write_settings_daemon(path: Path | None = None) -> Path:
    """Write the daemon's settings.json with full deny list."""
    target = path or PATHS.settings_daemon
    target.parent.mkdir(parents=True, exist_ok=True)
    content = {
        "disableAllHooks": True,
        "permissions": {"deny": DEFAULT_DENY},
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(content, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target
