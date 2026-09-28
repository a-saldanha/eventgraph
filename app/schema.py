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
from typing import Literal, Optional

from pydantic import BaseModel, Field

IdType = Literal["email", "phone", "handle", "none"]
ParticipantRole = Literal["sender", "recipient", "cc", "member"]
# Kinds a participant can be classified as at parse time. "unknown" defers the
# person/not-person decision to the resolution phase rather than guessing.
ParticipantKind = Literal[
    "person", "group", "org", "system", "self", "placeholder", "unknown"
]


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


class Participant(BaseModel):
    """One party on a message: a typed handle parsed from a header or export.

    `raw` keeps the exact original text (invisible marks included) for provenance;
    `id_value` is the normalized identifier used for must-linking in resolution.
    `kind` may be "unknown" when the parser can't decide — that is resolved later,
    not guessed here.
    """

    display_name: Optional[str] = None
    id_type: IdType = "none"
    id_value: Optional[str] = None
    raw: str = ""
    role: ParticipantRole = "member"
    kind: ParticipantKind = "unknown"
    descriptors: list[str] = Field(default_factory=list)


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
    participants: list[Participant] = Field(default_factory=list)
    subject: Optional[str] = None
    body: str = ""
    notes: str = ""  # ORACLE — not for the pipeline
    provenance: Provenance
    content_hash: str = ""
    # Canonical item id when this item is an exact/near duplicate of another. The
    # duplicate is retained in full for provenance; extraction/counts run over
    # canonicals only, so copies never inflate totals. Set by the dedup stage.
    duplicate_of: Optional[str] = None

    @property
    def senders(self) -> list[Participant]:
        return [p for p in self.participants if p.role == "sender"]

    @property
    def addressees(self) -> list[Participant]:
        return [p for p in self.participants if p.role in ("recipient", "cc")]

    @property
    def sender_display(self) -> str:
        """Best human label for the first sender, for the UI and retrieval text."""
        for p in self.senders:
            return p.display_name or p.raw or p.id_value or ""
        return ""

    @property
    def recipients_display(self) -> str:
        parts = [p.display_name or p.raw or p.id_value or "" for p in self.addressees]
        return ", ".join(x for x in parts if x)

    def emails(self) -> list[str]:
        return [p.id_value for p in self.participants if p.id_type == "email" and p.id_value]

    def compute_hash(self) -> str:
        """Exact-duplicate key over normalized body + sender + subject."""
        norm = _normalize_for_hash(f"{self.sender_display}\n{self.subject or ''}\n{self.body}")
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
