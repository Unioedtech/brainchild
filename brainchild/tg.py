"""Telegram client. urllib only.

Single-user bot — every inbound message is checked against owner_chat_id before
reaching the LLM. /pause /resume /status are handled here directly so they
work even when the LLM layer is overloaded.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from brainchild.config import PATHS

log = logging.getLogger("brainchild.tg")

MAX_CHUNK = 3900            # safety margin under TG's 4096
LONG_POLL_TIMEOUT = 30
SOCKET_TIMEOUT = LONG_POLL_TIMEOUT + 5
MAX_FILE_BYTES = 20 * 1024 * 1024


class TGClient:
    def __init__(self, token: str, owner_chat_id: int | None = None) -> None:
        self.token = token
        self.owner = owner_chat_id
        self._api = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._seen: deque[int] = deque(maxlen=200)
        self._offset = self._load_offset()
        self._last_send_ts: dict[int, deque[float]] = {}
        self._send_lock = threading.Lock()

    # ---- low-level HTTP ------------------------------------------------------

    def _post(self, method: str, **params) -> dict:
        url = f"{self._api}/{method}"
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        ).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"error": body}
            if e.code == 429:
                retry = (payload.get("parameters") or {}).get("retry_after", 1)
                log.warning("tg 429 retry_after=%s", retry)
                time.sleep(retry)
                return self._post(method, **params)
            log.error("tg %s http %s: %s", method, e.code, body[:300])
            return payload if isinstance(payload, dict) else {"ok": False}

    def _get(self, method: str, **params) -> dict:
        url = f"{self._api}/{method}?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    # ---- polling -------------------------------------------------------------

    def poll(self) -> list[dict]:
        """One long-poll call. Returns list of update dicts."""
        try:
            resp = self._get(
                "getUpdates",
                offset=self._offset,
                timeout=LONG_POLL_TIMEOUT,
                allowed_updates=json.dumps(["message"]),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.debug("tg poll transient: %s", e)
            time.sleep(2)
            return []
        if not resp.get("ok"):
            log.warning("tg poll not ok: %s", resp)
            return []
        updates = resp.get("result", [])
        # Dedup
        fresh = []
        for u in updates:
            uid = u.get("update_id")
            if uid in self._seen:
                continue
            self._seen.append(uid)
            fresh.append(u)
        if fresh:
            self._offset = max(u["update_id"] for u in fresh) + 1
            self._save_offset()
        return fresh

    def _load_offset(self) -> int:
        try:
            return int(PATHS.tg_offset.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_offset(self) -> None:
        PATHS.tg_offset.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATHS.tg_offset.with_suffix(".tmp")
        tmp.write_text(str(self._offset), encoding="utf-8")
        os.replace(tmp, PATHS.tg_offset)

    def ack_all(self) -> None:
        """Drain stale updates without processing (used on rotate/install)."""
        resp = self._get("getUpdates", timeout=0)
        if resp.get("ok") and resp.get("result"):
            self._offset = max(u["update_id"] for u in resp["result"]) + 1
            self._save_offset()

    # ---- send ----------------------------------------------------------------

    def send(self, chat_id: int, text: str, parse_mode: str = "HTML") -> None:
        self._rate_limit(chat_id)
        chunks = chunk_message(text)
        for chunk in chunks:
            resp = self._post(
                "sendMessage",
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                disable_web_page_preview="true",
            )
            if not resp.get("ok") and parse_mode:
                # Retry once as plain text
                log.warning("tg send parse fail, retry plain: %s", resp.get("description"))
                self._post("sendMessage", chat_id=chat_id, text=chunk)
            self._mark_sent(chat_id)

    def edit(self, chat_id: int, message_id: int, text: str, parse_mode: str = "HTML") -> dict:
        self._rate_limit(chat_id)
        return self._post(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text[:MAX_CHUNK],
            parse_mode=parse_mode,
            disable_web_page_preview="true",
        )

    def _rate_limit(self, chat_id: int) -> None:
        with self._send_lock:
            dq = self._last_send_ts.setdefault(chat_id, deque(maxlen=3))
            now = time.time()
            while dq and now - dq[0] > 3:
                dq.popleft()
            if len(dq) >= 3:
                wait = 3 - (now - dq[0])
                if wait > 0:
                    time.sleep(wait)
            dq.append(time.time())

    def _mark_sent(self, chat_id: int) -> None:
        pass  # already tracked in _rate_limit

    # ---- typing indicator ----------------------------------------------------

    @contextmanager
    def typing(self, chat_id: int):
        stop = threading.Event()
        def loop():
            while not stop.is_set():
                try:
                    self._post("sendChatAction", chat_id=chat_id, action="typing")
                except Exception:
                    pass
                stop.wait(4.5)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()

    # ---- file download -------------------------------------------------------

    def download_file(self, file_id: str, dest_dir: Path) -> Path:
        resp = self._get("getFile", file_id=file_id)
        if not resp.get("ok"):
            raise RuntimeError(f"getFile failed: {resp}")
        file_path = resp["result"]["file_path"]
        size = resp["result"].get("file_size") or 0
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file too large: {size} bytes")
        url = f"{self._file_base}/{file_path}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(file_path))
        dest = dest_dir / f"{int(time.time())}_{safe_name}"
        with urllib.request.urlopen(url, timeout=60) as r, dest.open("wb") as w:
            while True:
                buf = r.read(64 * 1024)
                if not buf:
                    break
                w.write(buf)
                if dest.stat().st_size > MAX_FILE_BYTES:
                    dest.unlink()
                    raise ValueError("file exceeded max size during download")
        return dest

    # ---- bot-info / pairing --------------------------------------------------

    def get_me(self) -> dict:
        return self._get("getMe")

    def discover_chat_id(self, timeout_sec: int = 300) -> int | None:
        """Poll up to timeout_sec for a /start. Returns chat_id or None."""
        deadline = time.time() + timeout_sec
        offset = 0
        while time.time() < deadline:
            try:
                resp = self._get("getUpdates", offset=offset, timeout=15)
            except Exception:
                time.sleep(2)
                continue
            for u in resp.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message") or {}
                if msg.get("text") == "/start":
                    return msg["chat"]["id"]
            if not resp.get("result"):
                time.sleep(2)
        return None


# ---- chunking ----------------------------------------------------------------

_CODE_FENCE = re.compile(r"^```(\w*)\s*$")


def chunk_message(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    """Split text into ≤max_len chunks, preserving code fences.

    Fence balance is tracked per-buffer (buf_in_code) separately from the
    global walk state (in_code) — so flushing a buffer that contains no
    fence at all never emits a phantom closing ```.
    """
    if len(text) <= max_len:
        return [text]
    out: list[str] = []
    in_code = False           # global walk state across the whole text
    buf_in_code = False       # does the current buffer have an open fence?
    code_lang = ""
    buf: list[str] = []
    buf_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1
        # Reserve 4 chars for the closing "\n```" if buffer has an open fence
        budget = max_len - 4 if buf_in_code else max_len
        # Flush BEFORE applying this line's fence toggle, using buf_in_code
        if buf and buf_len + line_len > budget:
            chunk = "\n".join(buf)
            if buf_in_code:
                chunk += "\n```"
            out.append(chunk)
            buf = []
            buf_len = 0
            buf_in_code = False
            if in_code:
                # reopen fence in next chunk so code stays formatted
                opener = f"```{code_lang}"
                buf.append(opener)
                buf_len = len(opener) + 1
                buf_in_code = True
        # Now process the line and update fence state
        m = _CODE_FENCE.match(line)
        if m:
            if not in_code:
                in_code = True
                buf_in_code = True
                code_lang = m.group(1)
            else:
                in_code = False
                buf_in_code = False
        buf.append(line)
        buf_len += line_len
    if buf:
        out.append("\n".join(buf))

    # Hard-split anything still too long (rare — a single line > max_len)
    final: list[str] = []
    for chunk in out:
        if len(chunk) <= max_len:
            final.append(chunk)
            continue
        for i in range(0, len(chunk), max_len):
            final.append(chunk[i : i + max_len])
    return final


# ---- markdown → HTML ---------------------------------------------------------

_MD_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def markdown_to_html(md: str) -> str:
    """Minimal markdown → Telegram-HTML."""
    placeholders: list[str] = []

    def stash(s: str) -> str:
        placeholders.append(s)
        return f"\x00{len(placeholders) - 1}\x00"

    # Fenced code blocks first
    def fence_sub(m: re.Match) -> str:
        lang = m.group(1)
        code = html.escape(m.group(2))
        cls = f' class="language-{lang}"' if lang else ""
        return stash(f"<pre><code{cls}>{code}</code></pre>")

    md = _MD_FENCE.sub(fence_sub, md)
    md = _MD_INLINE_CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), md)
    md = _MD_LINK.sub(
        lambda m: stash(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>'),
        md,
    )
    # Escape remaining text
    md = html.escape(md, quote=False)
    md = _MD_BOLD.sub(r"<b>\1</b>", md)
    md = _MD_ITALIC.sub(r"<i>\1</i>", md)
    # Restore placeholders
    def unstash(m: re.Match) -> str:
        return placeholders[int(m.group(1))]
    md = re.sub(r"\x00(\d+)\x00", unstash, md)
    return md
