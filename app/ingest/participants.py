"""Turn message headers and contact labels into typed `Participant`s.

Deterministic, no LLM. The goal is to classify *structurally* what a header field
is — a person, a group title, a role mailbox, a placeholder — so that downstream
resolution never treats "—" or "Acme (Downtown, Springfield)" as a person. When the
parser can't decide, it returns kind="unknown" and leaves the decision to Phase 4.
"""
from __future__ import annotations

import os
import re
from email.utils import getaddresses

from ..schema import Participant, ParticipantRole, SourceType

# --- normalization ---------------------------------------------------------
_INVISIBLE_RE = re.compile(r"[‎‏‪-‮⁦-⁩ ]")
_WS_RE = re.compile(r"\s+")


def strip_invisible(s: str) -> str:
    """Remove bidi / zero-width / nbsp marks used by exports, keep visible text."""
    return _WS_RE.sub(" ", _INVISIBLE_RE.sub("", s or "")).strip()


# --- lexicons (generic, not corpus-specific) -------------------------------
_PLACEHOLDERS = {"", "-", "—", "–", "n/a", "na", "none", "null", "unknown", "?", "tbd", "…"}

# Standard automated / shared role local-parts. A message from one of these is not
# a person even though it has a well-formed address.
_ROLE_LOCALPARTS = {
    "info", "support", "help", "helpdesk", "sales", "billing", "accounts",
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "notification", "marketing", "admin", "administrator", "registration",
    "register", "team", "contact", "hello", "office", "mailer-daemon",
    "postmaster", "bounce", "bounces", "automated", "auto", "system", "service",
}

# Words that mark a contact-label token as a descriptor, not part of the name.
_ROLE_WORDS = {
    "phd", "prof", "professor", "dr", "mr", "mrs", "ms", "sir", "madam",
    "student", "advisor", "supervisor", "chair", "coordinator", "manager",
    "director", "head", "dean", "lab", "group", "team", "dept", "department",
    "friend", "family", "colleague", "ex-colleague", "roommate", "neighbour",
    "neighbor", "office", "work", "home", "personal",
}

# Cues that a no-email token is an organization rather than a person.
_ORG_CUES = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "gmbh", "plc", "co",
    "company", "global", "services", "service", "solutions", "systems", "group",
    "holdings", "bank", "airways", "airlines", "air", "university", "college",
    "institute", "school", "hotel", "center", "centre", "agency", "society",
    "council", "committee", "logistics", "travel", "tours", "foundation",
}


# --- phone normalization ---------------------------------------------------
try:
    import phonenumbers  # optional; see requirements.txt
except ImportError:  # pragma: no cover - exercised only when the lib is absent
    phonenumbers = None

# Fallback country-code map for when phonenumbers isn't installed. Only used to
# stamp a national number with the configured region's calling code.
_REGION_CC = {"US": "1", "CA": "1", "GB": "44", "IN": "91", "FI": "358",
              "DE": "49", "FR": "33", "AE": "971", "QA": "974"}


def _default_region() -> str:
    return os.getenv("PHONE_REGION", "US").upper()


_PHONE_SHAPE_RE = re.compile(r"^\+?[\d][\d\s().\-]{6,}$")


def looks_like_phone(s: str) -> bool:
    return bool(_PHONE_SHAPE_RE.match(s.strip())) and sum(c.isdigit() for c in s) >= 7


def normalize_phone(raw: str, region: str | None = None) -> str | None:
    """Normalize a phone number to E.164, or None if it can't be parsed."""
    region = region or _default_region()
    raw = raw.strip()
    if phonenumbers is not None:
        try:
            num = phonenumbers.parse(raw, None if raw.startswith("+") else region)
            if phonenumbers.is_valid_number(num):
                return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            return None
        return None
    # Deterministic fallback: compact to +<digits>, applying the region code when
    # the number is written in national form.
    intl = raw.startswith("+") or raw.startswith("00")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    if not intl:
        digits = _REGION_CC.get(region, "") + digits
    return "+" + digits


# --- helpers ---------------------------------------------------------------
def is_placeholder(s: str) -> bool:
    return strip_invisible(s).lower() in _PLACEHOLDERS


def _role_localpart(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in _ROLE_LOCALPARTS


def _looks_like_org(name: str) -> bool:
    toks = re.findall(r"[a-zA-Z]+", name.lower())
    return any(t in _ORG_CUES for t in toks)


def _title_token(t: str) -> bool:
    """A plausible name token: alphabetic and capitalized, not a role word."""
    return t[:1].isupper() and t[1:].islower() and t.lower() not in _ROLE_WORDS and t.isalpha()


def split_name_descriptors(label: str) -> tuple[str, list[str]]:
    """Separate an owner-added contact label into (name, descriptors).

    Keeps the trailing run of name-like tokens as the name; leading acronyms,
    role words and institution tokens become descriptors. Returns an empty name
    when nothing trailing looks like a personal name (caller treats as unknown).
    """
    toks = label.split()
    if not toks:
        return "", []
    i = len(toks)
    while i > 0 and _title_token(toks[i - 1]):
        i -= 1
    name = " ".join(toks[i:])
    descriptors = [t for t in toks[:i] if t]
    return name, descriptors


def _classify_label(raw: str, role: ParticipantRole, group_title: str | None) -> Participant:
    """Classify a no-email header value (a name, handle, phone, or placeholder)."""
    clean = strip_invisible(raw)
    if is_placeholder(clean):
        return Participant(raw=raw, role=role, kind="placeholder")
    low = clean.lower()
    if group_title and clean == strip_invisible(group_title):
        return Participant(display_name=clean, raw=raw, role=role, kind="group")
    if re.search(r"\bself\b", low) or low.startswith("(self"):
        return Participant(display_name=clean, raw=raw, role=role, kind="self")
    if looks_like_phone(clean):
        e164 = normalize_phone(clean)
        return Participant(display_name=None, id_type="phone", id_value=e164, raw=raw,
                           role=role, kind="person" if e164 else "unknown")
    # organisation with a parenthesised location/comma stays one participant.
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", clean)
    if m and (_looks_like_org(m.group(1)) or "," in m.group(2)):
        return Participant(display_name=m.group(1).strip(), raw=raw, role=role,
                           kind="org", descriptors=[m.group(2).strip()])
    if _looks_like_org(clean):
        return Participant(display_name=clean, raw=raw, role=role, kind="org")
    name, descriptors = split_name_descriptors(clean)
    if name:
        return Participant(display_name=name, raw=raw, role=role, kind="person",
                           descriptors=descriptors)
    return Participant(display_name=clean, raw=raw, role=role, kind="unknown")


def _classify_address(display: str, email: str, role: ParticipantRole) -> Participant:
    """Classify a parsed (display, email) pair from an address header."""
    email = email.strip().lower()
    display = strip_invisible(display)
    if _role_localpart(email):
        return Participant(display_name=display or None, id_type="email", id_value=email,
                           raw=(f"{display} <{email}>" if display else email),
                           role=role, kind="system")
    name, descriptors = split_name_descriptors(display) if display else ("", [])
    return Participant(display_name=name or display or None, id_type="email", id_value=email,
                       raw=(f"{display} <{email}>" if display else email),
                       role=role, kind="person", descriptors=descriptors)


def _split_top_level(field: str) -> list[str]:
    """Split a list on commas that are not inside <>, (), or quotes."""
    out, depth, buf, quote = [], 0, [], False
    for ch in field:
        if ch in "\"'":
            quote = not quote
        elif not quote and ch in "<(":
            depth += 1
        elif not quote and ch in ">)":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0 and not quote:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def _parse_field(field: str, role: ParticipantRole, group_title: str | None) -> list[Participant]:
    # Keep invisible marks in the token text (Participant.raw); classification
    # strips them internally. This preserves provenance while matching on clean text.
    if not strip_invisible(field):
        return []
    if "@" in field:
        # Address list: getaddresses respects quotes and angle brackets.
        out = []
        for display, email in getaddresses([field]):
            if email and "@" in email:
                out.append(_classify_address(display, email, role))
            elif display or email:
                out.append(_classify_label(display or email, role, group_title))
        return out
    return [_classify_label(tok, role, group_title) for tok in _split_top_level(field)]


def _parse_issuer(field: str, role: ParticipantRole) -> list[Participant]:
    """A document issuer is one org; a trailing location becomes a descriptor note,
    not a separate participant (e.g. "Acme Air (booking office), Springfield")."""
    clean = strip_invisible(field)
    if not clean or is_placeholder(clean):
        return []
    segs = _split_top_level(clean)
    return [Participant(display_name=segs[0], raw=field, role=role, kind="org",
                        descriptors=segs[1:])]


def build_participants(
    source_type: SourceType,
    from_val: str = "",
    to_val: str = "",
    cc_val: str = "",
    group_title: str | None = None,
) -> list[Participant]:
    """Parse the sender / recipients / cc of one item into typed participants."""
    parts: list[Participant] = []
    if source_type == SourceType.PDF:
        parts += _parse_issuer(from_val, "sender")  # document issuer → one org
    else:
        parts += _parse_field(from_val, "sender", group_title)
    parts += _parse_field(to_val, "recipient", group_title)
    parts += _parse_field(cc_val, "cc", group_title)
    return parts


def mark_shared_mailboxes(items) -> None:
    """Flag an email address used by 3+ distinct display names as a shared mailbox.

    A shared/role address must not merge the people who use it, so we downgrade
    such participants to kind="system". Runs after all items are parsed.
    """
    names_by_email: dict[str, set[str]] = {}
    for it in items:
        for p in it.participants:
            if p.id_type == "email" and p.id_value and p.display_name:
                names_by_email.setdefault(p.id_value, set()).add(p.display_name.lower())
    shared = {e for e, names in names_by_email.items() if len(names) >= 3}
    if not shared:
        return
    for it in items:
        for p in it.participants:
            if p.id_type == "email" and p.id_value in shared and p.kind == "person":
                p.kind = "system"
