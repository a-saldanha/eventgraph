"""Chunk canonical SourceItems into conversation-grouped, token-budget-sized slices.

Design:
- Group by conversation_id, sort by timestamp.
- Pack messages into ~8k-token chunks with 5-message overlap at boundaries.
- Assign chunk-local ids m1..mN; keep a map back to real item ids.
- One line per message: <mID> <ISO time> <sender_pid> -> <recipient_pids>: <text>
- Prepend a participant table (pid, display_name, kind, descriptors).
- Strip boilerplate lines that appear verbatim in >= BOILERPLATE_MIN_COUNT items
  (env: BOILERPLATE_MIN_COUNT, default 5) — reduces token waste.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..schema import SourceItem

# Rough tokens-per-char estimate (conservative, covers unicode overhead).
_CHARS_PER_TOKEN = 3.5
# Target input token budget per chunk (8k tokens ~ 28k chars).
_CHUNK_TOKEN_TARGET = int(os.getenv("CHUNK_TOKEN_TARGET", "8000"))
_CHUNK_CHAR_BUDGET = int(_CHUNK_TOKEN_TARGET * _CHARS_PER_TOKEN)
# Overlap in messages when wrapping a conversation across chunks.
_OVERLAP_MSGS = 5
# Minimum number of items a line must appear in to be considered boilerplate.
_BOILERPLATE_MIN = int(os.getenv("BOILERPLATE_MIN_COUNT", "5"))


@dataclass
class Chunk:
    """One unit sent to the LLM for extraction."""

    chunk_id: str                         # e.g. "conv:chat1:0"
    conversation_id: str
    chunk_index: int
    participant_table: str                 # rendered text for the prompt
    message_lines: list[str]              # one entry per message, already rendered
    id_map: dict[str, str]                # local id (m1..) -> real item id
    # Reverse map: real item id -> local id (convenient for verify).
    item_id_map: dict[str, str] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return self.participant_table + "\n" + "\n".join(self.message_lines)

    @property
    def char_count(self) -> int:
        return len(self.full_text)

    @property
    def estimated_input_tokens(self) -> int:
        return max(1, int(self.char_count / _CHARS_PER_TOKEN))


def _pid_for(item: SourceItem, participant_index: dict) -> str:
    """Assign a stable short pid to the item's first sender within a conversation."""
    key = _sender_key(item)
    if key not in participant_index:
        n = len(participant_index) + 1
        participant_index[key] = f"p{n}"
    return participant_index[key]


def _sender_key(item: SourceItem) -> str:
    for p in item.senders:
        return (p.id_value or p.display_name or p.raw or "").lower().strip()
    return "__unknown__"


def _recipient_keys(item: SourceItem) -> list[str]:
    out = []
    for p in item.addressees:
        k = (p.id_value or p.display_name or p.raw or "").lower().strip()
        if k:
            out.append(k)
    return out


def _build_boilerplate_set(items: list[SourceItem]) -> set[str]:
    """Return lines that appear verbatim in >= _BOILERPLATE_MIN items."""
    if _BOILERPLATE_MIN <= 0:
        return set()
    line_counts: Counter = Counter()
    for it in items:
        seen_lines: set[str] = set()
        for line in it.body.splitlines():
            stripped = line.strip()
            if stripped and stripped not in seen_lines:
                seen_lines.add(stripped)
                line_counts[stripped] += 1
    return {line for line, count in line_counts.items() if count >= _BOILERPLATE_MIN}


def _strip_boilerplate(text: str, boilerplate: set[str]) -> str:
    if not boilerplate:
        return text
    out = []
    for line in text.splitlines():
        if line.strip() not in boilerplate:
            out.append(line)
    return "\n".join(out)


def _fmt_ts(item: SourceItem) -> str:
    if item.timestamp:
        return item.timestamp.isoformat(timespec="seconds")
    return item.timestamp_raw[:20] if item.timestamp_raw else "?"


def _render_participant_table(pid_table: dict[str, dict]) -> str:
    """Render the participant table header for the prompt."""
    if not pid_table:
        return ""
    lines = ["## Participants"]
    lines.append("pid | display_name | kind | descriptors")
    lines.append("----|--------------|------|------------")
    for pid, info in sorted(pid_table.items()):
        desc = "; ".join(info.get("descriptors", []))
        lines.append(f"{pid} | {info['display_name']} | {info['kind']} | {desc}")
    return "\n".join(lines)


def chunk_items(items: list[SourceItem]) -> list[Chunk]:
    """Group canonical items by conversation_id and pack into ~8k-token chunks."""
    # Build a global boilerplate set over all items first.
    boilerplate = _build_boilerplate_set(items)

    # Group by conversation_id; items with no conv_id each form their own group.
    groups: dict[str, list[SourceItem]] = {}
    for it in items:
        key = it.conversation_id or f"__solo_{it.id}"
        groups.setdefault(key, []).append(it)

    # Sort each group by timestamp (None last).
    # Use a timezone-naive sentinel so naive and aware datetimes don't mix.
    _SENTINEL = __import__("datetime").datetime.max.replace(tzinfo=None)

    def ts_key(x: SourceItem):
        ts = x.timestamp
        if ts is None:
            return _SENTINEL
        # Strip timezone info for sort key to avoid naive/aware comparison errors.
        if ts.tzinfo is not None:
            from datetime import timezone
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts

    for key in groups:
        groups[key].sort(key=ts_key)

    chunks: list[Chunk] = []

    for conv_id, conv_items in sorted(groups.items()):
        # Build a participant index for this conversation.
        # key -> pid, separate from display info
        pid_keys: dict[str, str] = {}      # sender_key -> pid
        pid_info: dict[str, dict] = {}     # pid -> display info

        def ensure_pid(key: str, item: SourceItem, participant_obj=None) -> str:
            if key not in pid_keys:
                n = len(pid_keys) + 1
                pid = f"p{n}"
                pid_keys[key] = pid
                # Pull display info from participants
                if participant_obj:
                    display = participant_obj.display_name or participant_obj.id_value or participant_obj.raw or key
                    kind = participant_obj.kind
                    desc = list(participant_obj.descriptors)
                else:
                    display = key
                    kind = "unknown"
                    desc = []
                pid_info[pid] = {"display_name": display, "kind": kind, "descriptors": desc}
            return pid_keys[key]

        # Pre-scan all participants in conversation to build stable pid table.
        for it in conv_items:
            for p in it.participants:
                k = (p.id_value or p.display_name or p.raw or "").lower().strip()
                if k:
                    ensure_pid(k, it, p)

        part_table = _render_participant_table(pid_info)
        part_table_chars = len(part_table) + 1  # +1 for newline

        # Render each message line.
        msg_lines: list[tuple[str, str]] = []  # (real_item_id, rendered_line)
        for it in conv_items:
            # Sender pid
            sk = _sender_key(it)
            sender_pid = pid_keys.get(sk, "p?")
            # Recipient pids
            rec_pids = []
            for rk in _recipient_keys(it):
                rec_pids.append(pid_keys.get(rk, "p?"))
            rec_str = ",".join(rec_pids) if rec_pids else "-"
            # Body with boilerplate stripped
            body = _strip_boilerplate(it.body, boilerplate)
            body = re.sub(r"\s+", " ", body).strip()
            body = body[:1200]  # hard cap per message to avoid blowup
            ts = _fmt_ts(it)
            # Placeholder local id filled later
            line = f"__MID__ {ts} {sender_pid} -> {rec_str}: {body}"
            msg_lines.append((it.id, line))

        # Pack into chunks respecting the budget.
        chunk_index = 0
        start = 0
        n = len(msg_lines)

        while start < n:
            # Assign local ids starting from m1 for this chunk.
            id_map: dict[str, str] = {}        # local_id -> real_item_id
            item_id_map: dict[str, str] = {}   # real_item_id -> local_id
            rendered_lines: list[str] = []
            char_count = part_table_chars

            for offset, (real_id, template) in enumerate(msg_lines[start:], start=1):
                local_id = f"m{offset}"
                line = template.replace("__MID__", f"<{local_id}>", 1)
                line_chars = len(line) + 1  # +1 for newline
                if char_count + line_chars > _CHUNK_CHAR_BUDGET and rendered_lines:
                    break
                id_map[local_id] = real_id
                item_id_map[real_id] = local_id
                rendered_lines.append(line)
                char_count += line_chars

            end = start + len(rendered_lines)
            chunk = Chunk(
                chunk_id=f"{conv_id}:{chunk_index}",
                conversation_id=conv_id,
                chunk_index=chunk_index,
                participant_table=part_table,
                message_lines=rendered_lines,
                id_map=id_map,
                item_id_map=item_id_map,
            )
            chunks.append(chunk)
            chunk_index += 1

            if end >= n:
                break
            # Overlap: step back _OVERLAP_MSGS messages.
            start = max(start + 1, end - _OVERLAP_MSGS)

    return chunks
