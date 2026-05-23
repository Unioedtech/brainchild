"""Install wizard. 12 steps, checkpointed, resumable.

Plain stdlib UI. Each answer is saved atomically so the wizard can resume
from any point if interrupted.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from brainchild import claude_runner, synthesis, tg, vault
from brainchild.config import PATHS, Config, DEFAULT_VAULT
from brainchild.install import bot_setup
from brainchild.security import set_secret, write_settings_daemon

SEPARATOR = "─" * 60
TOTAL_STEPS = 12


def run() -> None:
    PATHS.ensure()
    write_settings_daemon()
    state = _load_state()

    _print_welcome(state)

    try:
        _step_2_vault(state)
        _step_3_identity(state)
        _step_4_day_to_day(state)
        _step_5_priorities(state)
        _step_6_numbers(state)
        _step_7_push_back(state)
        _step_8_never(state)
        _step_9_rhythm(state)
        _step_10_files(state)
        _step_11_telegram(state)
        _step_12_synthesize_and_finish(state)
    except KeyboardInterrupt:
        print("\n\n[install paused] Re-run `brainchild install` to resume.")
        sys.exit(130)


# ---- state checkpointing -----------------------------------------------------

def _load_state() -> dict[str, Any]:
    if PATHS.init_state.exists():
        try:
            data = json.loads(PATHS.init_state.read_text())
            if data.get("step") != "done":
                ans = input(
                    f"\nFound an unfinished install (stopped at {data.get('step', 'unknown')}). "
                    f"Resume? [Y/n] "
                ).strip().lower()
                if ans in ("n", "no"):
                    bak = PATHS.init_state.with_suffix(".json.bak")
                    PATHS.init_state.rename(bak)
                    return _empty_state()
                return data
            else:
                ans = input(
                    "\nBrainchild is already installed. Reconfigure? [y/N] "
                ).strip().lower()
                if ans not in ("y", "yes"):
                    sys.exit(0)
                return _empty_state()
        except Exception:
            pass
    return _empty_state()


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "started_at": datetime.now().isoformat(),
        "step": "start",
        "answers": {},
        "files": [],
        "telegram": {},
        "vault_path": str(DEFAULT_VAULT),
    }


def _save(state: dict, step: str) -> None:
    state["step"] = step
    state["updated_at"] = datetime.now().isoformat()
    PATHS.init_state.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATHS.init_state.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, PATHS.init_state)


# ---- UI helpers --------------------------------------------------------------

def _header(num: int, title: str) -> None:
    print(f"\n[{num}/{TOTAL_STEPS}] {title}")


def _multi_line() -> str:
    """Read until empty line. Returns joined text."""
    lines: list[str] = []
    while True:
        try:
            line = input("  " if lines else "> ")
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _single_line(prompt: str = "> ") -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    a = input(prompt + suffix).strip().lower()
    if not a:
        return default
    return a in ("y", "yes")


def _normalize_path(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    s = s.replace("\\ ", " ")
    s = os.path.expanduser(s)
    return os.path.abspath(s)


# ---- steps -------------------------------------------------------------------

def _print_welcome(state: dict) -> None:
    print(SEPARATOR)
    print("  Brainchild install")
    print("")
    print("  ~10 minutes. Edits only ~/.brainchild/ and your vault.")
    print("  Press Ctrl+C any time — progress is saved per step.")
    print(SEPARATOR)


def _step_2_vault(state: dict) -> None:
    if state.get("step") in ("done",) or "vault_path" in state["answers"]:
        return
    _header(2, "Vault location")
    print(f"  Where should your vault live? (default: {DEFAULT_VAULT})")
    print(f"  Press Enter for default, or paste an absolute path.")
    home = Path.home().resolve()
    while True:
        v = _single_line()
        if not v:
            target = Path(DEFAULT_VAULT)
            break
        candidate = Path(v).expanduser()
        if not candidate.is_absolute():
            # Treat relative input as a folder under home (never write to cwd)
            candidate = home / candidate.name
            print(f"  → interpreted as: {candidate}")
        try:
            candidate.resolve().relative_to(home)
        except ValueError:
            print(f"  ✗ vault must live under {home}. Try again.")
            continue
        target = candidate
        break
    state["vault_path"] = str(target)
    state["answers"]["vault_path"] = state["vault_path"]
    _save(state, "vault")


def _step_3_identity(state: dict) -> None:
    if "identity" in state["answers"]:
        return
    _header(3, "Who are you?")
    name = _single_line("  Name: ")
    role = _single_line("  One-line role / what you do: ")
    state["answers"]["identity"] = {"name": name, "role": role}
    _save(state, "identity")


def _step_4_day_to_day(state: dict) -> None:
    if "day_to_day" in state["answers"]:
        return
    _header(4, "Day-to-day")
    print("  Describe what your week actually looks like — projects, meetings,")
    print("  clients, deadlines. (Multi-line. Empty line to finish.)")
    state["answers"]["day_to_day"] = _multi_line()
    _save(state, "day_to_day")


def _step_5_priorities(state: dict) -> None:
    if "priorities" in state["answers"]:
        return
    _header(5, "Current priorities")
    print("  Top 3-5 things on your plate right now. One per line.")
    print("  Empty line to finish.")
    state["answers"]["priorities"] = _multi_line()
    _save(state, "priorities")


def _step_6_numbers(state: dict) -> None:
    if "numbers" in state["answers"]:
        return
    _header(6, "Numbers you track")
    print("  Revenue targets, deadlines, KPIs, streaks — anything numerical")
    print("  worth keeping on a scoreboard. (Empty to skip.)")
    state["answers"]["numbers"] = _multi_line()
    _save(state, "numbers")


PUSHBACK_OPTIONS = [
    "vague goals",
    "procrastination signals",
    "rationalizing avoidance",
    "scope creep / new ideas before shipping",
    "sycophantic self-talk",
    "skipping basics (food, sleep, exercise)",
    "running away from a hard conversation",
    "inflating progress numbers",
]


def _step_7_push_back(state: dict) -> None:
    if "push_back" in state["answers"]:
        return
    _header(7, "When should the agent push back?")
    print("  Pick all that apply (comma-separated numbers).")
    for i, opt in enumerate(PUSHBACK_OPTIONS, 1):
        print(f"    {i}. {opt}")
    print(f"    {len(PUSHBACK_OPTIONS) + 1}. all of the above")
    print(f"    {len(PUSHBACK_OPTIONS) + 2}. never push back")
    raw = _single_line()
    picks = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok.isdigit():
            continue
        n = int(tok)
        if n == len(PUSHBACK_OPTIONS) + 1:
            picks = list(PUSHBACK_OPTIONS)
            break
        if n == len(PUSHBACK_OPTIONS) + 2:
            picks = []
            break
        if 1 <= n <= len(PUSHBACK_OPTIONS):
            picks.append(PUSHBACK_OPTIONS[n - 1])
    state["answers"]["push_back"] = picks
    print(f"  selected: {', '.join(picks) if picks else '(none)'}")
    _save(state, "push_back")


def _step_8_never(state: dict) -> None:
    if "never_go_there" in state["answers"]:
        return
    _header(8, "What should I never bring up?")
    print("  Topics, framings, or words that are off-limits. (Empty to skip.)")
    state["answers"]["never_go_there"] = _multi_line()
    _save(state, "never")


def _step_9_rhythm(state: dict) -> None:
    if "rhythm" in state["answers"]:
        return
    _header(9, "Daily rhythm")
    print("  Format: HH:MM (24h). Leave blank to skip any window.")
    am = _single_line("  AM briefing time: ")
    midday = _single_line("  Midday briefing time: ")
    precraft = _single_line("  Pre-craft (deep-work) briefing time: ")
    night = _single_line("  Night wrap-up time: ")
    state["answers"]["rhythm"] = {
        "am": am or None, "midday": midday or None,
        "precraft": precraft or None, "night": night or None,
    }
    _save(state, "rhythm")


def _step_10_files(state: dict) -> None:
    if state["files"] and state.get("step") in ("files", "telegram", "synth", "done"):
        return
    _header(10, "Context files")
    print("  Drop any files that flesh out who you are: bios, brand docs,")
    print("  project briefs, business plans, anything. One path per line.")
    print("  Empty line when done. Drag-drop into the terminal works.")
    while True:
        raw = _single_line()
        if not raw:
            break
        try:
            p = Path(_normalize_path(raw))
        except Exception:
            print(f"    ✗ couldn't parse: {raw!r}")
            continue
        if not p.exists():
            print(f"    ✗ not found: {p}")
            continue
        if not p.is_file():
            print(f"    ✗ not a file: {p}")
            continue
        size = p.stat().st_size
        if size > 50 * 1024 * 1024:
            print(f"    ✗ too large: {size // (1024*1024)} MB (max 50)")
            continue
        if size > 5 * 1024 * 1024:
            if not _yes_no(f"    {p.name} is {size // (1024*1024)} MB — include? ", False):
                continue
        state["files"].append({"path": str(p), "size": size})
        print(f"    + added {p.name} ({size // 1024} KB)")
        _save(state, "files")
    _save(state, "files")


def _step_11_telegram(state: dict) -> None:
    if state.get("telegram", {}).get("chat_id"):
        return
    _header(11, "Telegram")
    if "voice" not in state["answers"]:
        state["answers"]["voice"] = _yes_no(
            "  Enable voice notes? (downloads ~180MB on first use) ", True,
        )
    token, chat_id = bot_setup.pair_telegram()
    state["telegram"] = {"chat_id": chat_id}
    set_secret("telegram_token", token)
    _save(state, "telegram")


def _step_12_synthesize_and_finish(state: dict) -> None:
    if state.get("step") == "done":
        print("\n  Already complete.")
        return
    _header(12, "Synthesizing your vault")
    cfg = _build_config(state)
    _write_config(cfg)

    # Pre-flight: verify claude is actually authenticated (needed by daemon)
    print("\n  Checking Claude Code authentication…")
    ok, msg = claude_runner.preflight(cfg)
    if not ok:
        print(f"  ✗ {msg}")
        print("\n  Wizard saved your progress. After you fix the above,")
        print("  re-run:  python -m brainchild install")
        sys.exit(2)
    print(f"  ✓ {msg}")

    print("\n  Building your vault from your answers (instant, no LLM call)…")
    print("  Dropped files copied to vault/inbox/ for the daemon to use later.\n")

    spinner_stop = threading.Event()
    threading.Thread(target=_spinner, args=(spinner_stop,), daemon=True).start()

    try:
        manifest = synthesis.synthesize(
            qa=state["answers"],
            files=[Path(f["path"]) for f in state["files"]],
            vault_path=cfg.vault_path,
            cfg=cfg,
            progress_cb=lambda m: _status(m),
        )
    finally:
        spinner_stop.set()
        time.sleep(0.2)
        sys.stdout.write("\r" + " " * 60 + "\r")

    print(f"\n  ✓ vault written to {cfg.vault_path}")
    if manifest.get("warnings"):
        print("  ! synthesis warnings:")
        for w in manifest["warnings"]:
            print(f"      - {w}")

    _register_service()
    _save(state, "done")
    _print_final(cfg, state)


def _build_config(state: dict) -> Config:
    cfg = Config()
    cfg.vault_path = Path(state["vault_path"])
    cfg.owner_chat_id = state["telegram"]["chat_id"]
    cfg.voice_enabled = bool(state["answers"].get("voice"))
    rhythm = state["answers"].get("rhythm") or {}
    cfg.briefing_am = rhythm.get("am") or None
    cfg.briefing_midday = rhythm.get("midday") or None
    cfg.briefing_precraft = rhythm.get("precraft") or None
    cfg.briefing_night = rhythm.get("night") or None
    return cfg


def _write_config(cfg: Config) -> None:
    """Write config.toml using TOML LITERAL strings (single quotes).

    Double-quoted TOML strings interpret backslash escapes — so a Windows
    path like C:\\Users\\mahaj writes as "C:\\Users\\..." which TOML reads
    as \\U... = unicode escape = parse error.  Literal strings ('...')
    skip escape processing entirely.
    """
    PATHS.config_file.parent.mkdir(parents=True, exist_ok=True)
    def lit(s: str) -> str:
        # Literal strings can't contain a single quote. None known here,
        # but guard by converting any to underscore as a last-resort.
        return "'" + str(s).replace("'", "_") + "'"
    lines = [
        f'vault_path = {lit(cfg.vault_path)}',
        f'claude_model = {lit(cfg.claude_model)}',
        f'claude_timeout_sec = {cfg.claude_timeout_sec}',
        f'owner_chat_id = {cfg.owner_chat_id}',
        f'voice_enabled = {str(cfg.voice_enabled).lower()}',
    ]
    if cfg.briefing_am: lines.append(f'briefing_am = {lit(cfg.briefing_am)}')
    if cfg.briefing_midday: lines.append(f'briefing_midday = {lit(cfg.briefing_midday)}')
    if cfg.briefing_precraft: lines.append(f'briefing_precraft = {lit(cfg.briefing_precraft)}')
    if cfg.briefing_night: lines.append(f'briefing_night = {lit(cfg.briefing_night)}')
    lines.append(f'day_rollover = {lit(cfg.day_rollover)}')
    PATHS.config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _register_service() -> None:
    from brainchild.install import service_register
    try:
        service_register.register()
    except Exception as e:
        print(f"  ! service registration failed: {e}")
        print("    you can start the daemon manually: python -m brainchild")


def _spinner(stop: threading.Event) -> None:
    chars = "|/-\\"
    i = 0
    while not stop.is_set():
        sys.stdout.write(f"\r  {chars[i % 4]} working… ")
        sys.stdout.flush()
        i += 1
        stop.wait(0.15)


def _status(msg: str) -> None:
    sys.stdout.write(f"\r  → {msg}".ljust(60) + "\n")
    sys.stdout.flush()


def _print_final(cfg: Config, state: dict) -> None:
    print()
    print(SEPARATOR)
    print("  Brainchild installed.\n")
    print(f"  Vault:    {cfg.vault_path}")
    print(f"  Logs:     {PATHS.logs_dir}")
    print(f"  Audit:    {PATHS.audit_dir}")
    print(f"  Telegram: chat_id {cfg.owner_chat_id}")
    print()
    print("  CLI:")
    print("    brainchild status        see daemon state")
    print("    brainchild pause         pause processing")
    print("    brainchild resume        start it again")
    print("    brainchild audit tail    inspect tool actions")
    print("    brainchild uninstall     remove everything")
    print()
    print("  Your agent will message you on Telegram within the next hour.")
    print("  Everything in your vault is plain markdown — edit anything by hand.")
    print(SEPARATOR)
