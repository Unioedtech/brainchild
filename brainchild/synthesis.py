"""Vault synthesis: Q&A + dropped files → fully populated vault.

ONE Opus call. No digest phase. Q&A + all file content (truncated) goes
straight into the architect prompt. If the call fails, fall back to a
minimal template vault from Q&A alone so install always completes.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from brainchild import claude_runner, prompts, vault
from brainchild.config import Config

log = logging.getLogger("brainchild.synthesis")

PER_FILE_MAX_CHARS = 30_000   # truncate each file
TOTAL_FILES_MAX_CHARS = 80_000 # cap combined file content
ARCHITECT_TIMEOUT_SEC = 360   # 6 minutes — generous for Pro rate limits + Opus
ARCHITECT_MODEL = "opus"


class SynthesisError(RuntimeError):
    pass


def synthesize(
    qa: dict[str, Any],
    files: list[Path],
    vault_path: Path,
    cfg: Config,
    progress_cb=None,
) -> dict[str, Any]:
    """Run the full pipeline. Writes vault files. Returns the manifest."""

    def progress(msg: str) -> None:
        log.info("synth: %s", msg)
        if progress_cb:
            progress_cb(msg)

    # Inline file content — no separate digest calls
    file_blocks: list[str] = []
    total_chars = 0
    for f in files:
        progress(f"reading {f.name}")
        text = _read_text(f)
        if not text.strip():
            progress(f"  ⚠ {f.name} had no extractable text — skipping")
            continue
        if len(text) > PER_FILE_MAX_CHARS:
            text = text[:PER_FILE_MAX_CHARS]
        if total_chars + len(text) > TOTAL_FILES_MAX_CHARS:
            remaining = TOTAL_FILES_MAX_CHARS - total_chars
            if remaining < 5000:
                progress(f"  ⚠ {f.name} skipped — combined file budget exhausted")
                break
            text = text[:remaining]
            progress(f"  ⚠ {f.name} truncated to fit budget")
        file_blocks.append(f'<file name="{f.name}">\n{text}\n</file>')
        total_chars += len(text)
        progress(f"  ✓ {f.name} ({len(text)//1024}KB included)")

    # v1: template path is the DEFAULT. Claude Code CLI cannot reliably emit
    # structured JSON (verified empirically across many installs). Template
    # uses every Q&A answer the user provided; daemon learns more via LOG.
    # Files dropped at install time get stashed in inbox/ for later use.
    progress("building your vault from Q&A answers")
    manifest = _fallback_manifest(qa, vault_path)

    # Stash dropped files into vault inbox so they're not lost
    inbox = vault_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for fb_index, raw_file in enumerate(files):
        try:
            dest = inbox / raw_file.name
            if not dest.exists():
                dest.write_bytes(raw_file.read_bytes())
        except Exception as e:
            log.warning("inbox stash failed for %s: %s", raw_file, e)

    progress("validating manifest")
    _validate(manifest)

    progress("writing vault files")
    _write_manifest(manifest, vault_path)

    return manifest


# ---- Phase A: per-file digest ------------------------------------------------

def _read_text(path: Path) -> str:
    # Forward to the existing reader below
    return _read_text_impl(path)


def _read_text_impl(path: Path) -> str:
    try:
        if path.suffix.lower() in (".md", ".txt", ".markdown", ".org"):
            return path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".pdf":
            return _read_pdf(path)
        if path.suffix.lower() in (".docx",):
            return _read_docx(path)
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("read %s failed: %s", path, e)
        return ""


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        return "\n\n".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        log.warning("pypdf not installed; skipping PDF %s", path)
        return ""


def _read_docx(path: Path) -> str:
    try:
        import docx  # type: ignore
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    except ImportError:
        log.warning("python-docx not installed; skipping %s", path)
        return ""


# ---- Phase B: architect call -------------------------------------------------

def _call_architect(qa: dict[str, Any], file_blocks: list[str], cfg: Config) -> dict:
    system_prompt = prompts.load("vault_architect.txt")
    qa_block = json.dumps(qa, indent=2)
    files_section = "\n\n".join(file_blocks) if file_blocks else "<none>"
    user = (
        f"<qa>\n{qa_block}\n</qa>\n\n"
        f"<files>\n{files_section}\n</files>\n\n"
        f"Emit the JSON object now. Start with the opening curly brace."
    )
    out = claude_runner.run(
        user, cfg, trigger="synth-architect",
        cwd=Path.home(),
        system_prompt=system_prompt,
        extra_args=["--model", ARCHITECT_MODEL],
        timeout_override=ARCHITECT_TIMEOUT_SEC,
    )
    log.info("architect raw output (first 1000c): %s", (out or "")[:1000])
    parsed = _extract_json(out)
    if not parsed:
        raise SynthesisError(f"architect returned unparseable output: {out[:600]}")
    return parsed


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip code fences if any
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


# ---- Phase C: validation -----------------------------------------------------

BANNED_SLUGS = {"work", "personal", "projects", "misc", "general", "stuff",
                "tasks", "side", "main", "other"}

BANNED_PHRASES = (
    "as an ai", "i'd be happy to", "great question", "i cannot help",
    "happy to help", "as a large language model",
)

REQUIRED_HEADERS = {
    "agent/PERSONA.md": ["## Identity", "## Voice", "## How to be",
                         "## Push-back rules", "## Never-go-there",
                         "## Domain context"],
    "agent/LIVE.md": ["## Today", "## Active streams", "## Open inputs",
                      "## Energy", "## Recent decisions"],
    "agent/BACKLOG.md": ["## Parked streams", "## Ideas", "## Someday",
                        "## Deferred decisions"],
    "agent/SCOREBOARD.md": ["## Targets", "## Streaks", "## Pressure clocks"],
    "agent/briefing-recipes.md": ["## AM", "## Midday", "## Pre-craft", "## Night"],
    "agent/push-back-rules.md": ["## Hard fights", "## Soft fights", "## Never fight"],
}


def _validate(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise SynthesisError("manifest is not an object")
    for k in ("streams", "files", "warnings"):
        if k not in manifest:
            keys = list(manifest.keys())
            raise SynthesisError(
                f"manifest missing key: {k}. got keys: {keys}. "
                f"(this usually means Claude returned the wrong shape — "
                f"retry the wizard, or check ~/.brainchild/logs/daemon.log)"
            )

    files = {f["path"]: f["content"] for f in manifest["files"]}

    # All 7 agent/ files present
    required = {"agent/PERSONA.md", "agent/LIVE.md", "agent/BACKLOG.md",
                "agent/SCOREBOARD.md", "agent/LOG.md",
                "agent/briefing-recipes.md", "agent/push-back-rules.md"}
    missing = required - files.keys()
    if missing:
        raise SynthesisError(f"missing required files: {sorted(missing)}")

    # Stream count 3–7 (or fewer with warning)
    streams = manifest["streams"]
    if not (1 <= len(streams) <= 7):
        raise SynthesisError(f"stream count out of range: {len(streams)}")

    # Banned slug check
    for s in streams:
        if s["slug"].lower() in BANNED_SLUGS:
            raise SynthesisError(f"banned generic stream slug: {s['slug']}")

    # Headers
    for path, headers in REQUIRED_HEADERS.items():
        content = files.get(path, "")
        for h in headers:
            if h not in content:
                raise SynthesisError(f"{path} missing required header: {h}")

    # LIVE today has 3 numbered items
    live = files["agent/LIVE.md"]
    today_section = _section(live, "## Today")
    numbered = re.findall(r"^\s*[1-9]\.\s+\S", today_section, re.MULTILINE)
    if len(numbered) < 3:
        raise SynthesisError(f"LIVE.md ## Today must have 3 numbered items, found {len(numbered)}")

    # Banned phrases
    for path, content in files.items():
        low = content.lower()
        for phrase in BANNED_PHRASES:
            if phrase in low:
                raise SynthesisError(f"{path} contains banned phrase: {phrase!r}")

    # Stream READMEs present
    stream_files = {s["slug"]: f"streams/{s['slug']}/README.md" for s in streams}
    for slug, path in stream_files.items():
        if path not in files:
            raise SynthesisError(f"missing stream README: {path}")


def _section(text: str, header: str) -> str:
    """Return body under a header until the next ## (or EOF)."""
    pattern = re.compile(
        rf"^{re.escape(header)}.*?^(?=## |\Z)",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(text)
    return m.group(0) if m else ""


# ---- Fallback template (when Claude call fails) ------------------------------

def _fallback_manifest(qa: dict[str, Any], vault_path: Path) -> dict:
    """Build a minimal but valid vault from Q&A alone. No LLM call."""
    name = (qa.get("identity") or {}).get("name") or "<unknown>"
    role = (qa.get("identity") or {}).get("role") or "<unknown — fill in>"
    day_to_day = qa.get("day_to_day") or "<unknown — fill in>"
    priorities_raw = (qa.get("priorities") or "").strip()
    priority_lines = [p.strip("- ").strip() for p in priorities_raw.split("\n") if p.strip()]
    while len(priority_lines) < 3:
        priority_lines.append("<unknown — capture first thing tomorrow>")
    push_back = qa.get("push_back") or []
    never = qa.get("never_go_there") or "<none specified>"
    numbers = qa.get("numbers") or ""
    rhythm = qa.get("rhythm") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    push_back_list = "\n".join(f"- {p}" for p in push_back) if push_back else "- <none specified>"

    persona = f"""---
type: persona
owner: {name}
---

# Persona

## Identity
- Name: {name}
- Role: {role}

## Voice
- Tone: <fill in — terse / warm / formal / etc.>
- Forbidden phrases: <fill in if any>

## How to be
- Lead with the answer
- Cite a file or a number or stay quiet

## Push-back rules
{push_back_list}

## Never-go-there
- {never}

## Domain context
- Day-to-day: {day_to_day}
"""

    live = f"""---
type: live
---

# Live

## Today ({today})
1. {priority_lines[0]}
2. {priority_lines[1]}
3. {priority_lines[2]}

## Active streams
- <fill in as you go>

## Open inputs (awaiting)
- <none yet>

## Energy / constraints
- <fill in your hours>

## Recent decisions
- <none yet>
"""

    backlog = """---
type: backlog
---

# Backlog

## Parked streams
- <none>

## Ideas
- <none>

## Someday/maybe
- <none>

## Deferred decisions
- <none>
"""

    scoreboard = f"""---
type: scoreboard
---

# Scoreboard

## Targets
| Metric | Current | Target | Deadline | Source |
|---|---|---|---|---|
| <fill in> | <?> | <?> | <?> | qa.numbers |

## Streaks / cadence
- <none yet>

## Pressure clocks
- <fill in>

<!-- Raw numbers from install Q&A:
{numbers}
-->
"""

    log_md = "---\ntype: log\n---\n\n# Log\n<!-- append-only, newest on top -->\n"

    am = rhythm.get("am") or "07:30"
    midday = rhythm.get("midday") or "13:00"
    precraft = rhythm.get("precraft") or "21:00"
    night = rhythm.get("night") or "23:30"

    briefings = f"""# Briefing recipes

## AM
- Fires: {am}
- Lead with: top 3 from LIVE Today
- Ask: What's the single hardest thing today?
- Avoid: yesterday's noise
- Tone: brisk

## Midday
- Fires: {midday}
- Lead with: drift from Today
- Ask: Still on the top item?
- Avoid: new ideas
- Tone: audit

## Pre-craft
- Fires: {precraft}
- Lead with: which stream gets deep time
- Ask: <none>
- Avoid: status
- Tone: silent runway

## Night
- Fires: {night}
- Lead with: shipped vs slipped
- Ask: push-back for tomorrow?
- Avoid: scoreboards if bad day
- Tone: honest, short
"""

    push_rules = f"""# Push-back rules

## Hard fights
{push_back_list}

## Soft fights
- Vague goals → demand specifics

## Never fight
- {never}
"""

    # Derive minimal streams from priorities
    streams = []
    for i, p in enumerate(priority_lines[:3]):
        if "<unknown" in p:
            continue
        slug = re.sub(r"[^a-z0-9-]", "-", p.lower())[:40].strip("-") or f"stream-{i+1}"
        streams.append({"slug": slug, "title": p[:60], "rationale": "qa.priorities"})
    if len(streams) < 3:
        for n in range(len(streams), 3):
            streams.append({
                "slug": f"unnamed-stream-{n+1}",
                "title": f"Stream {n+1} (fill in)",
                "rationale": "placeholder — rename me",
            })

    files = [
        {"path": "agent/PERSONA.md", "content": persona},
        {"path": "agent/LIVE.md", "content": live},
        {"path": "agent/BACKLOG.md", "content": backlog},
        {"path": "agent/SCOREBOARD.md", "content": scoreboard},
        {"path": "agent/LOG.md", "content": log_md},
        {"path": "agent/briefing-recipes.md", "content": briefings},
        {"path": "agent/push-back-rules.md", "content": push_rules},
    ]
    for s in streams:
        files.append({
            "path": f"streams/{s['slug']}/README.md",
            "content": f"# {s['title']}\n\n## Why this is a stream\n{s['rationale']}\n\n## Open threads\n- <fill in>\n\n## Last touched\n{today}\n",
        })

    return {
        "streams": streams,
        "files": files,
        "warnings": [
            "Vault built from FALLBACK TEMPLATE — Claude synthesis call failed or timed out.",
            "Edit agent/PERSONA.md, agent/LIVE.md, agent/SCOREBOARD.md by hand to flesh out.",
            "Re-run synthesis later with: python -m brainchild install (it will resume + redo synthesis).",
        ],
    }


# ---- Phase D: write ----------------------------------------------------------

def _write_manifest(manifest: dict, vault_path: Path) -> None:
    vault.ensure_vault(vault_path)
    for f in manifest["files"]:
        target = vault_path / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        vault.atomic_write(target, f["content"])
    # Also create stream folders explicitly (already done by README writes)
    for s in manifest["streams"]:
        (vault_path / "streams" / s["slug"]).mkdir(parents=True, exist_ok=True)
