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

    # Architecture:
    # - Full PDF/doc text → agent/CONTEXT-<slug>.md (verbatim, NEVER truncated)
    # - Auto-extracted numbers/dates → SCOREBOARD.md targets (always in snapshot)
    # - PERSONA hard-requires Read of CONTEXT files before plans/decisions
    # - Snapshot stays lean (~6KB), bot reads CONTEXT on demand via tools
    progress("reading dropped files")
    context_files: list[dict[str, Any]] = []   # full verbatim documents
    auto_facts: list[dict[str, Any]] = []      # extracted numbers/dates
    for f in files:
        text = _read_text(f)
        if not text.strip():
            progress(f"  ⚠ {f.name} had no extractable text — skipped")
            continue
        slug = re.sub(r"[^a-z0-9-]", "-", f.stem.lower())[:50].strip("-") or "doc"
        context_files.append({
            "slug": slug,
            "name": f.name,
            "text": text,
        })
        extracted = _auto_extract_facts(text, f.name)
        auto_facts.extend(extracted)
        progress(f"  ✓ {f.name} → CONTEXT-{slug}.md ({len(text)//1024}KB, {len(extracted)} facts extracted)")

    progress("building your vault from Q&A + verbatim file contents")
    manifest = _fallback_manifest(
        qa, vault_path,
        context_files=context_files,
        auto_facts=auto_facts,
    )

    # Also stash raw files in inbox/ so user can re-reference originals
    inbox = vault_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for raw_file in files:
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


# ---- Auto-extraction: deterministic regex, no LLM ----------------------------

_RE_RUPEE = re.compile(r"(₹|Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)\s?(cr|crore|lakh|lakhs|L|k|K|M|million)?", re.IGNORECASE)
_RE_DOLLAR = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s?(k|K|M|million|B|billion)?")
_RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_RE_DATE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b|\bQ[1-4]\s*\d{0,4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_RE_DEADLINE = re.compile(r"(?:by|before|deadline|due|target)[:\s]+([^.\n]{4,80})", re.IGNORECASE)
_RE_TARGET_LINE = re.compile(r"^[\s\-•*]*([^:\n]+?)[:=]\s*(.+?)$", re.MULTILINE)


def _auto_extract_facts(text: str, source: str) -> list[dict[str, Any]]:
    """Deterministic fact extraction — no LLM. Returns list of dict rows.

    Prefers structured 'Metric: value' lines over loose money regexes,
    since the former give clean metric names and the latter create noisy
    rows with phrase fragments as metric names.
    """
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(metric: str, value: str, deadline: str = "") -> None:
        metric = metric.strip().strip("-•* ").rstrip(":")
        value = value.strip().rstrip(",.")
        if not metric or not value or len(metric) > 60 or len(value) > 60:
            return
        key = (metric + value).lower()
        if key in seen:
            return
        seen.add(key)
        facts.append({
            "metric": metric,
            "value": value,
            "deadline": deadline.strip()[:30],
            "source": source,
        })

    # Pass 1: clean "Metric: value" lines (highest signal)
    for m in _RE_TARGET_LINE.finditer(text):
        metric, value = m.group(1), m.group(2)
        if (any(c.isdigit() for c in value)
            and len(metric) <= 60 and len(value) <= 60
            and "http" not in value
            and not metric.lstrip().startswith("#")):
            # Try to extract a deadline from value
            dl_match = _RE_DATE.search(value)
            deadline = dl_match.group(0) if dl_match else ""
            add(metric, value, deadline)

    # Pass 2: bullet lines with ₹/$ — only if not already captured
    for line in text.split("\n"):
        line_str = line.strip().lstrip("-•* ")
        if not line_str or len(line_str) > 120:
            continue
        money_match = _RE_RUPEE.search(line_str) or _RE_DOLLAR.search(line_str)
        if not money_match:
            continue
        # Split on the money: text before = metric, text including money = value
        prefix = line_str[:money_match.start()].rstrip(":= ").strip()
        if not prefix or len(prefix) > 60:
            continue
        value = line_str[money_match.start():].strip()
        dl_match = _RE_DATE.search(value)
        deadline = dl_match.group(0) if dl_match else ""
        add(prefix, value, deadline)

    return facts[:25]  # cap


def _surrounding_phrase(text: str, start: int, end: int, window: int = 40) -> str:
    """Grab a short phrase around a regex match for context."""
    left = max(0, start - window)
    right = min(len(text), end + window)
    phrase = text[left:right]
    # Trim to nearest sentence-ish boundary
    if left > 0:
        nl = phrase.find(" ")
        phrase = phrase[nl + 1:] if nl != -1 else phrase
    if right < len(text):
        nl = phrase.rfind(" ")
        phrase = phrase[:nl] if nl != -1 else phrase
    return phrase.strip().replace("\n", " ")[:80]


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

def _fallback_manifest(
    qa: dict[str, Any],
    vault_path: Path,
    context_files: list[dict[str, Any]] | None = None,
    auto_facts: list[dict[str, Any]] | None = None,
) -> dict:
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

    context_files = context_files or []
    auto_facts = auto_facts or []
    has_ctx = bool(context_files)
    ctx_filenames = [f"agent/CONTEXT-{c['slug']}.md" for c in context_files]
    ctx_pointer = (
        "\n## REQUIRED READING — non-negotiable\n"
        "Before generating ANY daily plan, briefing, decision recommendation, "
        "scoreboard update, or strategic advice, you MUST use the Read tool to "
        "load these files. They contain the user's playbook, financial "
        "guardrails, decision rules, vendor rules, target numbers — the ground "
        "truth that every plan must respect:\n"
        + "\n".join(f"  - {p}" for p in ctx_filenames)
        + "\n\nDo NOT shortcut. Do NOT rely on memory of these files between "
        "messages — Read them every time you build a plan. They are SHORT "
        "documents; reading them is cheap.\n"
        if has_ctx else ""
    )
    persona = f"""---
type: persona
owner: {name}
---

# Persona

## Identity
- Name: {name}
- Role: {role}

## Voice
- Tone: derived from CONTEXT-DOCS.md if present; else <fill in by hand>
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
{ctx_pointer}
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

    target_rows = []
    if numbers.strip():
        target_rows.append(f"| qa.numbers (raw) | — | — | — | install Q&A |")
    for fact in auto_facts:
        target_rows.append(
            f"| {fact['metric']} | — | {fact['value']} | {fact['deadline']} | {fact['source']} |"
        )
    if not target_rows:
        target_rows.append("| <fill in> | <?> | <?> | <?> | qa.numbers |")

    scoreboard = f"""---
type: scoreboard
---

# Scoreboard

## Targets
| Metric | Current | Target | Deadline | Source |
|---|---|---|---|---|
{chr(10).join(target_rows)}

## Streaks / cadence
- <none yet>

## Pressure clocks
- <fill in — see CONTEXT-*.md for deadlines and runway constraints>

<!-- Raw user-typed numbers from install Q&A:
{numbers}
-->
"""

    log_md = "---\ntype: log\n---\n\n# Log\n<!-- append-only, newest on top -->\n"

    am = rhythm.get("am") or "07:30"
    midday = rhythm.get("midday") or "13:00"
    precraft = rhythm.get("precraft") or "21:00"
    night = rhythm.get("night") or "23:30"

    ctx_read_line = (
        "- BEFORE generating this briefing, you MUST Read every agent/CONTEXT-*.md "
        "file. They define the playbook, financial guardrails, and decision rules "
        "every priority must respect.\n"
        if has_ctx else ""
    )
    briefings = f"""# Briefing recipes

## AM
- Fires: {am}
{ctx_read_line}- Lead with: top 3 priorities for today, derived from LIVE Today + CONTEXT-*.md guardrails
- Ask: What's the single hardest thing today?
- Avoid: yesterday's noise; suggestions that violate guardrails in CONTEXT-*.md
- Tone: brisk

## Midday
- Fires: {midday}
{ctx_read_line}- Lead with: drift from Today
- Ask: Still on the top item?
- Avoid: new ideas (those go to BACKLOG)
- Tone: audit

## Pre-craft
- Fires: {precraft}
- Lead with: which stream gets deep time + 3 lines of context for it
- Ask: <none>
- Avoid: status, numbers, anything that breaks state
- Tone: silent runway

## Night
- Fires: {night}
{ctx_read_line}- Lead with: shipped vs slipped — check against CONTEXT-*.md targets
- Ask: push-back for tomorrow?
- Avoid: scoreboards if bad day — name it once, move on
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

    # One CONTEXT file per dropped doc — verbatim, never truncated.
    # PERSONA + briefing-recipes hard-require the bot to Read these before
    # any plan/decision. They're at the vault root so Read with relative
    # path "agent/CONTEXT-<slug>.md" works from cwd=vault_path.
    for cf in context_files:
        body = (
            f"---\ntype: context-doc\nsource: {cf['name']}\n---\n\n"
            f"# {cf['name']}\n\n"
            f"<!-- VERBATIM CONTENT — this is ground truth. Read in full before any plan. -->\n\n"
            f"{cf['text']}\n"
        )
        files.append({"path": f"agent/CONTEXT-{cf['slug']}.md", "content": body})

    # Also write an INDEX so the bot can discover all CONTEXT files at once.
    if context_files:
        index_lines = ["---", "type: context-index", "---", "", "# Context index", "",
                       "All ground-truth documents. The bot MUST Read these before plans/decisions.", ""]
        for cf in context_files:
            index_lines.append(f"- agent/CONTEXT-{cf['slug']}.md ({cf['name']}, {len(cf['text'])//1024}KB)")
        files.append({"path": "agent/CONTEXT-INDEX.md", "content": "\n".join(index_lines) + "\n"})
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
