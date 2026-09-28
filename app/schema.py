"""Canonical ingestion schema.

Everything from every source normalizes to a `SourceItem`. Raw text is always
retained (grounding + entity-resolution + eval depend on it); compression happens
in a *separate* distillation layer that points back to these items, never by
mutating them. See decisions.md (D-compression).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    PDF = "pdf"
    IMAGE = "image"
    EXCEL_ROW = "excel_row"
    OTHER = "other"


class Provenance(BaseModel):
    """Where a SourceItem came from — enough to re-open the exact source."""

    batch_file: str
    item_index: int  # the `--- ITEM n ---` number within that file
    section: Optional[str] = None  # e.g. WhatsApp chat header this item sits under


class SourceItem(BaseModel):
    """One atomic unit of correspondence (an email, a WhatsApp message, a PDF, …).

    `body` is the raw (redacted) text — never summarized in place. `notes` is the
    redactor's human annotation; it is an EVAL ORACLE only and must never be fed
    into the extraction pipeline (it would pre-solve dedup/relevance).
    """

    id: str
    source_type: SourceType
    channel: str = ""
    conversation_id: str = ""  # groups items into a conversation/thread
    timestamp_raw: str = ""
    timestamp: Optional[datetime] = None  # normalized where parseable
    sender: str = ""
    recipients: str = ""
    subject: Optional[str] = None
    body: str = ""
    notes: str = ""  # ORACLE — not for the pipeline
    provenance: Provenance
    content_hash: str = ""

    def compute_hash(self) -> str:
        """Exact-duplicate key over normalized body + sender + subject."""
        norm = _normalize_for_hash(f"{self.sender}\n{self.subject or ''}\n{self.body}")
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()


_WS_RE = re.compile(r"\s+")
# Invisible bidi / formatting marks the WhatsApp export carries.
_INVISIBLE_RE = re.compile(r"[‎‏‪-‮⁦-⁩ ]")


def _normalize_for_hash(s: str) -> str:
    s = _INVISIBLE_RE.sub("", s or "")
    s = s.strip().lower()
    return _WS_RE.sub(" ", s)


def normalize_text(s: str) -> str:
    """Public normalizer used by dedup/near-dup and matching."""
    return _normalize_for_hash(s)
