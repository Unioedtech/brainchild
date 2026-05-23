"""Voice transcription pipeline.

ogg → wav (16kHz mono via imageio-ffmpeg) → whisper.cpp transcript.

Mac/Linux: pywhispercpp (wheel ships Metal/BLAS builds).
Windows: whisper-bin-x64.zip CLI from GitHub Releases.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from brainchild.config import PATHS, Config

log = logging.getLogger("brainchild.voice")

# Pinned model — small q5_0 ggml, Hinglish-capable
MODEL_NAME = "ggml-small-q5_0.bin"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"
MODEL_SIZE_MB = 180

WHISPER_WIN_RELEASE = "https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.4/whisper-bin-x64.zip"


def model_path(name: str = MODEL_NAME) -> Path:
    return PATHS.models_dir / name


def ensure_model(progress_cb=None) -> Path:
    """Download model on first use. progress_cb(percent, downloaded_mb, total_mb)."""
    dest = model_path()
    if dest.exists() and dest.stat().st_size > 10 * 1024 * 1024:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial")
    log.info("downloading whisper model %s → %s", MODEL_URL, tmp)
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, tmp.open("wb") as w:
        total = int(r.headers.get("Content-Length") or 0)
        downloaded = 0
        last_pct = -5
        while True:
            buf = r.read(64 * 1024)
            if not buf:
                break
            w.write(buf)
            downloaded += len(buf)
            if total and progress_cb:
                pct = int(downloaded * 100 / total)
                if pct - last_pct >= 5:
                    progress_cb(pct, downloaded // (1024 * 1024), total // (1024 * 1024))
                    last_pct = pct
    os.replace(tmp, dest)
    return dest


def _ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        from shutil import which
        path = which("ffmpeg")
        if not path:
            raise RuntimeError("ffmpeg not available; pip install imageio-ffmpeg")
        return path


def ogg_to_wav(src: Path) -> Path:
    """Convert ogg/opus to 16kHz mono WAV. Returns dest path."""
    dest = src.with_suffix(".wav")
    cmd = [
        _ffmpeg_bin(), "-y", "-loglevel", "error",
        "-i", str(src), "-ar", "16000", "-ac", "1", str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[:300]}")
    return dest


def transcribe(audio: Path, cfg: Config) -> str:
    """Top-level: take any audio path → return text."""
    if audio.suffix.lower() in (".oga", ".ogg", ".opus"):
        wav = ogg_to_wav(audio)
    else:
        wav = audio
    model = ensure_model()
    try:
        return _transcribe_pywhispercpp(wav, model)
    except Exception as e:
        log.warning("pywhispercpp path failed: %s", e)
        if sys.platform == "win32":
            return _transcribe_windows_cli(wav, model)
        raise


def _transcribe_pywhispercpp(wav: Path, model: Path) -> str:
    try:
        from pywhispercpp.model import Model  # type: ignore
    except ImportError:
        raise RuntimeError("pywhispercpp not installed (pip install pywhispercpp)")
    m = Model(str(model), n_threads=4)
    segments = m.transcribe(str(wav), language="auto")
    return " ".join(s.text.strip() for s in segments).strip()


def _transcribe_windows_cli(wav: Path, model: Path) -> str:
    """Use whisper.cpp main.exe (downloaded on first run)."""
    bin_dir = PATHS.install_dir / "whisper-bin"
    main_exe = bin_dir / "main.exe"
    if not main_exe.exists():
        _install_whisper_windows()
    cmd = [str(main_exe), "-m", str(model), "-f", str(wav), "-l", "auto", "-otxt"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(bin_dir))
    if proc.returncode != 0:
        raise RuntimeError(f"whisper.exe failed: {proc.stderr[:300]}")
    txt_path = wav.with_suffix(".wav.txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8", errors="replace").strip()
    return proc.stdout.strip()


def _install_whisper_windows() -> None:
    log.info("downloading whisper.cpp windows binaries")
    bin_dir = PATHS.install_dir / "whisper-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    zip_path = bin_dir / "whisper.zip"
    with urllib.request.urlopen(WHISPER_WIN_RELEASE, timeout=120) as r, zip_path.open("wb") as w:
        w.write(r.read())
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(bin_dir)
    zip_path.unlink()
