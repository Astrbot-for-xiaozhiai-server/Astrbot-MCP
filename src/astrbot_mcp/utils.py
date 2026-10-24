"""Small shared helpers used by service modules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_CHARSET_RE = re.compile(r"charset=([A-Za-z0-9_\-]+)", re.IGNORECASE)


def json_dumps(value: Any) -> str:
    """Serialize a value as readable, non-ASCII-escaped JSON."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def truncate_text(text: str, limit: int, suffix: str = "\n...[truncated]") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + suffix


def truncate_bytes(data: bytes, limit: int) -> tuple[bytes, bool]:
    if limit <= 0 or len(data) <= limit:
        return data, False
    return data[:limit], True


def decode_bytes(data: bytes, content_type: str | None = None) -> str:
    """Decode bytes using the declared charset, then common fallbacks."""
    if not data:
        return ""

    candidates: list[str] = []
    if content_type:
        match = _CHARSET_RE.search(content_type)
        if match:
            candidates.append(match.group(1))
    candidates.extend(["utf-8", "gb18030", "latin-1"])

    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def strip_html(
    html: str,
    *,
    base_url: str = "",
    max_links: int = 0,
) -> dict[str, Any]:
    """Extract readable text (and optionally links) from an HTML document."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title is not None:
        title = soup.title.get_text(" ", strip=True)

    for tag in soup(
        ["script", "style", "noscript", "template", "svg", "iframe", "canvas"]
    ):
        tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    raw_text = root.get_text("\n")
    lines = [line.strip() for line in raw_text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)

    links: list[dict[str, str]] = []
    if max_links > 0:
        seen_urls: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith(("javascript:", "mailto:", "#")):
                continue
            absolute = urljoin(base_url, href) if base_url else href
            if absolute in seen_urls:
                continue
            seen_urls.add(absolute)
            label = anchor.get_text(" ", strip=True)
            links.append({"url": absolute, "text": label[:200]})
            if len(links) >= max_links:
                break

    return {"title": title, "text": text, "links": links}


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"
