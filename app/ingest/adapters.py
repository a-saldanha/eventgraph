"""Upload adapters: turn a raw uploaded file into SourceItems.

Dispatch by extension. Text-native formats a user actually exports in bulk are
supported now (redacted `.md` batches, WhatsApp `.txt`, `.eml`, `.mbox`); binary
document formats (pdf/xlsx/images) are recognized but deferred to the LlamaParse/
OCR stage — they return a clear placeholder instead of failing the whole upload.
"""
from __future__ import annotations

import mailbox
import os
import re
import tempfile
from datetime import datetime
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..schema import Provenance, SourceItem, SourceType
from .markdown import _norm_subject, parse_batch_file, parse_timestamp

SUPPORTED_NOW = {".md", ".txt", ".eml", ".mbox", ".pdf"}
DEFERRED = {".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".csv"}


class UnsupportedUpload(ValueError):
    pass


def parse_upload(filename: str, data: bytes) -> list[SourceItem]:
    ext = Path(filename).suffix.lower()
    if ext == ".md":
        return _from_markdown(filename, data)
    if ext == ".txt":
        return _from_whatsapp_txt(filename, data)
    if ext == ".eml":
        return _from_eml(filename, data, index=1)
    if ext == ".mbox":
        return _from_mbox(filename, data)
    if ext == ".pdf":
        return _from_pdf(filename, data)
    if ext in DEFERRED:
        raise UnsupportedUpload(
            f"{ext} needs the OCR/spreadsheet stage — skipped for now."
        )
    raise UnsupportedUpload(f"unrecognized file type: {ext}")


# ---- PDF via LlamaParse ---------------------------------------------------
def _llama_key() -> str:
    """Read the LlamaParse key from the environment, or from this project's .env."""
    key = os.getenv("LLAMA_CLOUD_API_KEY", "")
    if key:
        return key
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        m = re.search(r"LLAMA_CLOUD_API_KEY=(\S+)", env.read_text())
        if m and m.group(1).startswith("llx-"):
            return m.group(1)
    return ""


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
# document-date patterns seen in tickets/invoices/visa letters (earliest wins)
_PDF_DATE_PATS = [
    (re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b"), "ymd"),                       # 2026-05-17
    (re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b"), "dmy"),                 # 19-02-2026, 19/03/2026
    (re.compile(r"\b(\d{1,2})[- ]?([A-Za-z]{3,9})[- ]?(20\d{2})\b"), "dMy"),          # 05Aug2026, 19-Feb-2026, 19 Feb 2026
    (re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b"), "Mdy"),            # February 19, 2026
]


def _sniff_pdf_date(text: str):
    """Best-effort document date from parsed PDF text → earliest plausible datetime."""
    from datetime import datetime

    found = []
    for pat, kind in _PDF_DATE_PATS:
        for m in pat.finditer(text):
            try:
                if kind == "ymd":
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif kind == "dmy":
                    d, mo, y = int(m[1]), int(m[2]), int(m[3])
                elif kind == "dMy":
                    d, mo, y = int(m[1]), _MONTHS.get(m[2][:3].lower(), 0), int(m[3])
                else:  # Mdy
                    mo, d, y = _MONTHS.get(m[1][:3].lower(), 0), int(m[2]), int(m[3])
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    found.append(datetime(y, mo, d))
            except (ValueError, IndexError):
                continue
    return min(found) if found else None


def _from_pdf(filename: str, data: bytes) -> list[SourceItem]:
    """Parse a PDF via LlamaParse (upload → poll → JSON), one SourceItem per page.

    Each page becomes a SourceItem; a document-level date sniffed from the text is
    stamped on every page so the pages place correctly on the event timeline.
    """
    import time
    import httpx

    key = _llama_key()
    if not key:
        raise UnsupportedUpload("PDF needs LLAMA_CLOUD_API_KEY (LlamaParse) — skipped.")
    base = "https://api.cloud.llamaindex.ai/api/v1/parsing"
    headers = {"Authorization": f"Bearer {key}", "accept": "application/json"}
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{base}/upload", headers=headers,
                   files={"file": (filename, data, "application/pdf")})
        r.raise_for_status()
        job_id = r.json()["id"]
        ok = False
        for _ in range(90):
            st = c.get(f"{base}/job/{job_id}", headers=headers).json().get("status", "").upper()
            if st in ("SUCCESS", "COMPLETED", "OK"):
                ok = True
                break
            if st in ("ERROR", "FAILED", "CANCELED"):
                raise UnsupportedUpload(f"LlamaParse failed on {filename}")
            time.sleep(2)
        if not ok:
            raise UnsupportedUpload(f"LlamaParse timed out on {filename}")
        result = c.get(f"{base}/job/{job_id}/result/json", headers=headers).json()

    stem = Path(filename).stem
    pages = result.get("pages") or []
    texts = [(page.get("md") or page.get("text") or "").strip() for page in pages]
    doc_date = _sniff_pdf_date("\n".join(texts))

    items: list[SourceItem] = []
    for i, text in enumerate(texts, start=1):
        if not text:
            continue
        it = SourceItem(
            id=f"{stem}#p{i}", source_type=SourceType.PDF,
            channel="file-upload", conversation_id=f"document:{stem}",
            timestamp=doc_date, timestamp_raw=(doc_date.date().isoformat() if doc_date else ""),
            sender="", recipients="", subject=filename, body=text,
            provenance=Provenance(batch_file=filename, item_index=i),
        )
        it.content_hash = it.compute_hash()
        items.append(it)
    if not items:  # nothing extracted → surface a clear message rather than silent
        raise UnsupportedUpload(f"LlamaParse returned no text for {filename}")
    return items


def _from_markdown(filename: str, data: bytes) -> list[SourceItem]:
    with tempfile.NamedTemporaryFile("wb", suffix=".md", delete=False) as f:
        f.write(data)
        tmp = f.name
    items = parse_batch_file(tmp)
    stem = Path(filename).stem
    for it in items:  # stable ids + provenance from the user's real filename
        it.provenance.batch_file = filename
        it.id = f"{stem}#{it.provenance.item_index}"
    return items


# ---- WhatsApp .txt export -------------------------------------------------
# iOS:     [18/03/26, 12:08:48 PM] Sender: message
# Android: 18/03/2026, 12:08 - Sender: message
_WA_IOS = re.compile(r"^\[(\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]?[Mm]?)\]\s*([^:]+?):\s?(.*)$")
_WA_AND = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?)\s*-\s*([^:]+?):\s?(.*)$")


def _from_whatsapp_txt(filename: str, data: bytes) -> list[SourceItem]:
    text = data.decode("utf-8", errors="replace")
    chat = Path(filename).stem
    items: list[SourceItem] = []
    cur: dict | None = None
    idx = 0

    def flush():
        nonlocal cur, idx
        if cur is None:
            return
        idx += 1
        raw_ts = cur["ts"]
        it = SourceItem(
            id=f"{chat}#{idx}", source_type=SourceType.WHATSAPP,
            channel=f"whatsapp:{chat}", conversation_id=f"whatsapp:{chat}",
            timestamp_raw=raw_ts,
            timestamp=parse_timestamp(SourceType.WHATSAPP, f"[{raw_ts}]"),
            sender=cur["sender"], recipients="",
            body="\n".join(cur["lines"]).strip(),
            provenance=Provenance(batch_file=filename, item_index=idx, section=chat),
        )
        it.content_hash = it.compute_hash()
        items.append(it)
        cur = None

    for line in text.splitlines():
        m = _WA_IOS.match(line) or _WA_AND.match(line)
        if m:
            flush()
            cur = {"ts": m.group(1).strip(), "sender": m.group(2).strip(), "lines": [m.group(3)]}
        elif cur is not None:
            cur["lines"].append(line)  # continuation of a multi-line message
    flush()
    return items


# ---- .eml / .mbox ---------------------------------------------------------
def _msg_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return str(msg.get_payload())


def _item_from_message(msg, filename: str, index: int) -> SourceItem:
    subject = msg.get("Subject", "") or None
    ts_raw = msg.get("Date", "") or ""
    try:
        ts = parsedate_to_datetime(ts_raw) if ts_raw else None
    except (TypeError, ValueError):
        ts = None
    it = SourceItem(
        id=f"{Path(filename).stem}#{index}", source_type=SourceType.EMAIL,
        channel="email", conversation_id=f"email-thread:{_norm_subject(subject or '')}",
        timestamp_raw=ts_raw, timestamp=ts,
        sender=msg.get("From", ""), recipients=msg.get("To", ""),
        subject=subject, body=_msg_body(msg).strip(),
        provenance=Provenance(batch_file=filename, item_index=index),
    )
    it.content_hash = it.compute_hash()
    return it


def _from_eml(filename: str, data: bytes, index: int) -> list[SourceItem]:
    return [_item_from_message(message_from_bytes(data), filename, index)]


def _from_mbox(filename: str, data: bytes) -> list[SourceItem]:
    with tempfile.NamedTemporaryFile("wb", suffix=".mbox", delete=False) as f:
        f.write(data)
        tmp = f.name
    box = mailbox.mbox(tmp)
    return [_item_from_message(m, filename, i + 1) for i, m in enumerate(box)]
