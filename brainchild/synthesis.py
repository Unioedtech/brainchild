"""Vault synthesis: Q&A + dropped files → fully populated vault.

Two-phase:
  A. Per-file digest (parallel claude calls, one per chunk) → file-level summary
  B. Vault architect (single claude call) → JSON manifest → files on disk
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from brainchild import claude_runner, prompts, vault
from brainchild.config import Config

log = logging.getLogger("brainchild.synthesis")

CHUNK_CHARS = 24_000          # ~6k tokens
CHUNK_OVERLAP = 800
MAX_FILE_PARALLEL = 3


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

    # Phase A: per-file digests
    digests: list[dict[str, Any]] = []
    for f in files:
        try:
            progress(f"reading {f.name}")
            digest = _digest_file(f, cfg)
            if digest:
                digests.append({"name": f.name, "digest": digest})
        except Exception as e:
            log.warning("digest failed for %s: %s", f, e)

    # Phase B: synthesis
    progress("composing vault")
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
    text = _read_text(path)
    if not text:
        return {}
    chunks = _chunk(text)
    digest_prompt = prompts.load("file_digest.txt")
    chunk_results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=MAX_FILE_PARALLEL) as pool:
        futures = [
            pool.submit(_digest_chunk, digest_prompt, chunk, cfg)
            for chunk in chunks
        ]
        for fut in as_completed(futures):
            try:
                d = fut.result()
                if d:
                    chunk_results.append(d)
            except Exception as e:
                log.warning("chunk digest failed: %s", e)

    merged = _merge_digests(chunk_results)
    return merged


def _digest_chunk(system_prompt: str, chunk: str, cfg: Config) -> dict[str, Any]:
    user = f"<chunk>\n{chunk}\n</chunk>"
    out = claude_runner.run(
        user, cfg, trigger="synth-digest",
        cwd=Path.home(), system_prompt=system_prompt,
    )
    return _extract_json(out)


def _merge_digests(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("identity_signals", "priorities", "numbers", "deadlines",
            "rhythm", "voice_samples", "vocabulary", "never_go_there")
    merged: dict[str, list] = {k: [] for k in keys}
    for c in chunks:
        for k in keys:
            for item in (c.get(k) or [])[:6]:
                if item not in merged[k]:
                    merged[k].append(item)
    for k in keys:
        merged[k] = merged[k][:12]
    return merged


def _chunk(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out = []
    i = 0
    while i < len(text):
        end = min(i + CHUNK_CHARS, len(text))
        out.append(text[i:end])
        if end >= len(text):
            break
        i = end - CHUNK_OVERLAP
    return out


def _read_text(path: Path) -> str:
    try:
        if path.suffix.lower() in (".md", ".txt", ".markdown", ".org"):
            return path.read_text(errors="replace")
        if path.suffix.lower() == ".pdf":
            return _read_pdf(path)
        if path.suffix.lower() in (".docx",):
            return _read_docx(path)
        # Best-effort text read for unknown extensions
        return path.read_text(errors="replace")
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
        cwd=Path.home(), system_prompt=system_prompt,
    )
    parsed = _extract_json(out)
    if not parsed:
        raise SynthesisError(f"architect returned unparseable output: {out[:400]}")
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
            raise SynthesisError(f"manifest missing key: {k}")

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
