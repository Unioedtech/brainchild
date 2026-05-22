"""Config + path resolution for Brainchild.

All filesystem paths the daemon uses are resolved here. Cross-platform via
pathlib + platform-aware caches.
"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


HOME = Path.home()
INSTALL_DIR = HOME / ".brainchild"
DEFAULT_VAULT = HOME / "brainchild-vault"


def _model_cache_dir() -> Path:
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "brainchild" / "models"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "brainchild" / "models"
        return HOME / "AppData" / "Local" / "brainchild" / "models"
    base = os.environ.get("XDG_CACHE_HOME") or str(HOME / ".cache")
    return Path(base) / "brainchild" / "models"


@dataclass
class Paths:
    install_dir: Path = INSTALL_DIR
    config_file: Path = INSTALL_DIR / "config.toml"
    secrets_dir: Path = INSTALL_DIR / "secrets"
    state_dir: Path = INSTALL_DIR / "state"
    logs_dir: Path = INSTALL_DIR / "logs"
    audit_dir: Path = INSTALL_DIR / "audit"
    tmp_dir: Path = INSTALL_DIR / "tmp"
    repo_dir: Path = INSTALL_DIR / "repo"
    pause_file: Path = INSTALL_DIR / "PAUSE"
    init_state: Path = INSTALL_DIR / "init-state.json"
    settings_daemon: Path = INSTALL_DIR / "settings-daemon.json"
    snapshot: Path = INSTALL_DIR / "state" / "snapshot.md"
    jobs_state: Path = INSTALL_DIR / "state" / "jobs.json"
    tg_offset: Path = INSTALL_DIR / "state" / "tg_offset"
    pending_log: Path = INSTALL_DIR / "state" / "pending" / "log-inbox.md"
    models_dir: Path = field(default_factory=_model_cache_dir)

    def ensure(self) -> None:
        """Create all directories with restrictive permissions."""
        for d in (
            self.install_dir, self.secrets_dir, self.state_dir,
            self.logs_dir, self.audit_dir, self.tmp_dir, self.models_dir,
            self.state_dir / "pending",
        ):
            d.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                try:
                    os.chmod(d, 0o700)
                except OSError:
                    pass


@dataclass
class Config:
    vault_path: Path = DEFAULT_VAULT
    claude_bin: str = "claude"
    claude_model: str = "opus"
    claude_timeout_sec: int = 180
    claude_max_tool_calls: int = 25
    owner_chat_id: int | None = None
    voice_enabled: bool = False
    voice_model: str = "small"  # tiny | base | small | medium
    briefing_am: str | None = None      # "HH:MM" 24h
    briefing_midday: str | None = None
    briefing_precraft: str | None = None
    briefing_night: str | None = None
    day_rollover: str = "02:00"
    snapshot_interval_sec: int = 300
    log_backfill_interval_sec: int = 90
    vault_backup_enabled: bool = False
    timezone: str = "Asia/Kolkata"

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        cfg = cls()
        # Flat key→attr mapping; nested sections allowed but we just merge
        flat: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                flat.update(v)
            else:
                flat[k] = v
        for k, v in flat.items():
            if hasattr(cfg, k):
                if k == "vault_path":
                    v = Path(os.path.expanduser(str(v)))
                setattr(cfg, k, v)
        return cfg


PATHS = Paths()


def is_paused() -> bool:
    return PATHS.pause_file.exists()


def system_label() -> str:
    """Short OS label used in launchd/systemd templates."""
    plat = platform.system().lower()
    if plat == "darwin":
        return "macos"
    if plat == "linux":
        return "linux"
    if plat == "windows":
        return "windows"
    return plat
