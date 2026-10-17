"""Operator-supplied device manuals, cached with source and digest."""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

MAX_MANUAL_BYTES = 20 * 1024 * 1024
MAX_CONTEXT_CHARS = 20_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _download(url: str) -> tuple[bytes, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("manual URL must be an HTTPS URL without embedded credentials")
    opener = build_opener(HTTPSHandler(), _NoRedirect())
    request = Request(url, headers={"User-Agent": "LabBench-HCP/0.1 manual-reader"})
    with opener.open(request, timeout=10) as response:
        content_type = response.headers.get_content_type()
        data = response.read(MAX_MANUAL_BYTES + 1)
    if len(data) > MAX_MANUAL_BYTES:
        raise ValueError("manual exceeds the 20 MiB limit")
    return data, content_type


def _text(data: bytes, mime: str) -> tuple[str, str | None]:
    if mime == "application/pdf" or data.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "Install labbench[manuals] to extract text from PDF manuals."
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages), None
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return data.decode("utf-8", errors="replace"), None
    return "", "This manual format has no text extractor; inspect the cached file."


def _read_manual_sync(source: str, cache_dir: Path) -> dict[str, Any]:
    if source.startswith("https://"):
        data, mime = _download(source)
        name = Path(urlsplit(source).path).name or "manual"
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"manual not found: {source}")
        if path.stat().st_size > MAX_MANUAL_BYTES:
            raise ValueError("manual exceeds the 20 MiB limit")
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        name = path.name
    digest = hashlib.sha256(data).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{digest[:16]}-{name.replace('/', '_')}"
    if not cached.exists():
        cached.write_bytes(data)
    extracted, warning = _text(data, mime)
    return {
        "source": source, "cached_path": str(cached), "sha256": digest,
        "mime_type": mime, "bytes": len(data),
        "text": extracted[:MAX_CONTEXT_CHARS], "text_truncated": len(extracted) > MAX_CONTEXT_CHARS,
        "warning": warning,
    }


async def read_manual(source: str, cache_dir: Path) -> dict[str, Any]:
    """Fetch a configured source and return bounded text for model context."""
    if source.startswith("https://"):
        return await asyncio.to_thread(_read_manual_sync, source, cache_dir)
    return _read_manual_sync(source, cache_dir)
