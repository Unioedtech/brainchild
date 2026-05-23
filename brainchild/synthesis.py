"""Vault synthesis: Q&A + dropped files → fully populated vault.

Two-phase:
  A. Per-file digest (one Haiku call per file, sequential) → file-level summary
  B. Vault architect (one Opus call) → JSON manifest → files on disk

Design choices:
- Haiku for digests because extracting identity/priorities from text doesn't
  need Opus and Haiku is ~10× faster on the Pro plan.
- One call per file (not chunked). Files truncated to MAX_FILE_CHARS.
- Sequential to avoid Pro-tier rate-limit spikes.
- A failing digest is skipped with a warning — never blocks the architect.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from brainchild import claude_runner, prompts, vault
from brainchild.config import Config

log = logging.getLogger("brainchild.synthesis")

MAX_FILE_CHARS = 60_000       # truncate large files; tail rarely adds signal
DIGEST_TIMEOUT_SEC = 90       # Haiku is fast; 90s is generous
ARCHITECT_TIMEOUT_SEC = 240   # Opus + bigger context, allow up to 4 min
DIGEST_MODEL = "haiku"
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

    # Phase A: per-file digests — Haiku, sequential, one call per file
    digests: list[dict[str, Any]] = []
    for f in files:
        started = time.monotonic()
        try:
            progress(f"reading {f.name}")
            digest = _digest_file(f, cfg)
            if digest:
                digests.append({"name": f.name, "digest": digest})
                progress(f"  ✓ {f.name} digested in {int(time.monotonic() - started)}s")
            else:
                progress(f"  ⚠ {f.name} produced no signal — skipped")
        except Exception as e:
            log.warning("digest failed for %s: %s", f, e)
            progress(f"  ⚠ {f.name} digest failed ({type(e).__name__}) — skipped, continuing")

    # Phase B: synthesis — Opus, single call
    progress(f"composing vault from Q&A + {len(digests)} file digest(s)")
    manifest = _call_architect(qa, digests, cfg)

    # Phase C: validate
    progress("validating manifest")
    _validate(manifest)

    # Phase D: write files
    progress("writing vault files")
    _write_manifest(manifest, vault_path)

    return manifest


# ---- Phase A: per-file digest ------------------------------------------------

def _digest_file(path: Path, cfg: Config) -> dict[str, Any]:
    """One Haiku call per file. Truncate over MAX_FILE_CHARS."""
    text = _read_text(path)
    if not text.strip():
        return {}
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS]
        log.info("truncated %s to %d chars", path.name, MAX_FILE_CHARS)
    digest_prompt = prompts.load("file_digest.txt")
    user = f"<file name=\"{path.name}\">\n{text}\n</file>"
    out = claude_runner.run(
        user, cfg, trigger="synth-digest",
        cwd=Path.home(),
        system_prompt=digest_prompt,
        extra_args=["--model", DIGEST_MODEL],
        timeout_override=DIGEST_TIMEOUT_SEC,
    )
    return _extract_json(out)


def _read_text(path: Path) -> str:
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

def _call_architect(qa: dict[str, Any], digests: list[dict], cfg: Config) -> dict:
    system_prompt = prompts.load("vault_architect.txt")
    qa_block = json.dumps(qa, indent=2)
    digests_block = json.dumps(digests, indent=2)
    user = (
        f"<qa>\n{qa_block}\n</qa>\n\n"
        f"<files>\n{digests_block}\n</files>\n\n"
        f"Emit the JSON object now."
    )
    out = claude_runner.run(
        user, cfg, trigger="synth-architect",
        cwd=Path.home(),
        system_prompt=system_prompt,
        extra_args=["--model", ARCHITECT_MODEL],
        timeout_override=ARCHITECT_TIMEOUT_SEC,
    )
    log.info("architect raw output (first 800c): %s", (out or "")[:800])
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
