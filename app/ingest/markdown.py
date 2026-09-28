"""Parse the redacted `--- ITEM n ---` markdown batches into SourceItems.

Deterministic, no network, no LLM. Handles the real quirks in the corpus:
  - multi-line `from / to:` blocks
  - fenced ``` body ``` blocks (with blank lines / nested markup inside)
  - `## Chat:` section headers that group WhatsApp items into conversations
  - per-source timestamp formats (RFC-2822 email vs `[dd/mm/yy, h:mm:ss AM]` vs PDF)
The redactor's `notes:` field is captured but flagged as eval-only (never fed
downstream — see decisions.md).
"""
from __future__ import annotations

import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from ..schema import Provenance, SourceItem, SourceType

_ITEM_RE = re.compile(r"^---\s*ITEM\s+(\d+)\s*---\s*$")
_SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^```")
_KEY_RE = re.compile(
    r"^(source_type|channel|conversation|timestamp|from ?/ ?to|from/to|subject|"
    r"body_format|body|notes)\s*:\s?(.*)$",
    re.IGNORECASE,
)
_KNOWN_KEYS = {
    "source_type", "channel", "conversation", "timestamp", "from / to",
    "from/to", "subject", "body_format", "body", "notes",
}


def parse_batch_file(path: str | Path) -> list[SourceItem]:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    items: list[SourceItem] = []

    section: Optional[str] = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        sec = _SECTION_RE.match(line)
        if sec and not _ITEM_RE.match(line):
            section = _clean_section(sec.group(1))
            i += 1
            continue
        m = _ITEM_RE.match(line)
        if not m:
            i += 1
            continue
        item_index = int(m.group(1))
        i += 1
        block: list[str] = []
        while i < n and not _ITEM_RE.match(lines[i]) and not _SECTION_RE.match(lines[i]):
            block.append(lines[i])
            i += 1
        items.append(_parse_block(block, item_index, path.name, section))
    return items


def _clean_section(s: str) -> str:
    # strip surrounding backticks and trailing "(N messages)" counts
    s = s.strip().strip("`").strip()
    s = re.sub(r"\s*\(\d+\s+messages?\)\s*$", "", s)
    s = re.sub(r"^Chat:\s*", "", s, flags=re.IGNORECASE)
    return s.strip().strip("`").strip()


def _parse_block(block: list[str], item_index: int, batch_file: str, section: Optional[str]) -> SourceItem:
    fields: dict[str, str] = {}
    body_lines: list[str] = []
    cur_key: Optional[str] = None
    j = 0
    while j < len(block):
        line = block[j]
        km = _KEY_RE.match(line)
        key = km.group(1).lower().replace(" ", "").replace("/", "") if km else None
        # normalize "from / to" / "from/to" -> "fromto"
        if km and (km.group(1).lower().replace(" ", "") in ("from/to", "fromto") or "from" in km.group(1).lower()):
            key = "fromto"
        if km and km.group(1).lower() == "body":
            # body: consume the following fenced block
            j += 1
            # skip to opening fence (there may be a blank line)
            while j < len(block) and not _FENCE_RE.match(block[j].strip()):
                if block[j].strip():  # non-empty, non-fence -> inline body (rare)
                    break
                j += 1
            if j < len(block) and _FENCE_RE.match(block[j].strip()):
                j += 1  # past opening fence
                while j < len(block) and not _FENCE_RE.match(block[j].strip()):
                    body_lines.append(block[j])
                    j += 1
                j += 1  # past closing fence
            cur_key = "body"
            continue
        if km:
            cur_key = key
            fields[cur_key] = km.group(2).rstrip()
        elif cur_key and cur_key != "body":
            # continuation line (e.g. the indented `to:` under `from / to:`)
            fields[cur_key] = (fields.get(cur_key, "") + "\n" + line.strip()).strip()
        j += 1

    body = "\n".join(body_lines).strip("\n")
    source_type = _coerce_type(fields.get("source_type", ""))
    sender, recipients = _split_fromto(fields.get("fromto", ""))
    channel = fields.get("channel", "").strip()
    subject = (fields.get("subject", "") or "").strip() or None
    ts_raw = fields.get("timestamp", "").strip()

    item = SourceItem(
        id=f"{Path(batch_file).stem}#{item_index}",
        source_type=source_type,
        channel=channel,
        conversation_id=_conversation_id(source_type, channel, section, subject),
        timestamp_raw=ts_raw,
        timestamp=parse_timestamp(source_type, ts_raw),
        sender=sender,
        recipients=recipients,
        subject=subject,
        body=body,
        notes=(fields.get("notes", "") or "").strip(),
        provenance=Provenance(batch_file=batch_file, item_index=item_index, section=section),
    )
    item.content_hash = item.compute_hash()
    return item


def _coerce_type(raw: str) -> SourceType:
    raw = raw.strip().lower()
    for t in SourceType:
        if raw.startswith(t.value):
            return t
    return SourceType.OTHER


def _split_fromto(raw: str) -> tuple[str, str]:
    sender, recipients = "", ""
    for part in raw.split("\n"):
        p = part.strip()
        low = p.lower()
        if low.startswith("from:"):
            sender = p[5:].strip()
        elif low.startswith("to:"):
            recipients = (recipients + ", " + p[3:].strip()).strip(", ")
        elif low.startswith(("reply-to:", "cc:")):
            continue
        elif not sender:  # bare value, treat as sender
            sender = p
    return sender, recipients


# Either a "Re:/Fwd:/Fw:" prefix (separator required, longest-first so Fwd isn't
# clipped to Fw) or a bracket tag like [RESEND]/[URGENT]/[REMINDER].
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd|fw)\s*[:\-]\s*|\[(?:resend|urgent|reminder)\]\s*)",
    re.IGNORECASE,
)


def _norm_subject(subject: str) -> str:
    s = subject or ""
    prev = None
    while prev != s:  # strip stacked prefixes: "Re: Fwd: ..."
        prev = s
        s = _SUBJECT_PREFIX_RE.sub("", s).strip()
    return s.lower()


def _conversation_id(stype: SourceType, channel: str, section: Optional[str], subject: Optional[str]) -> str:
    if stype == SourceType.WHATSAPP:
        return f"whatsapp:{section or channel}".strip()
    if stype == SourceType.EMAIL and subject:
        return f"email-thread:{_norm_subject(subject)}"
    return channel or (section or "")


# ---------------------------------------------------------------------------
# Timestamp parsing — tolerant, per source
# ---------------------------------------------------------------------------
_WA_FORMATS = ("[%d/%m/%y, %I:%M:%S %p]", "[%d/%m/%Y, %I:%M:%S %p]",
               "[%d/%m/%y, %H:%M:%S]", "[%d/%m/%y, %I:%M %p]")


def parse_timestamp(stype: SourceType, raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw or raw.lower().startswith(("n/a", "undated", "none")):
        return None
    if stype == SourceType.EMAIL:
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            pass
    if stype == SourceType.WHATSAPP:
        cleaned = raw.replace(" ", " ").replace(" ", " ")
        for fmt in _WA_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
    # Generic fallback (PDF/Excel/misc): dateutil, tolerating trailing tz words.
    try:
        from dateutil import parser as dparser

        return dparser.parse(raw, fuzzy=True)
    except Exception:
        return None
