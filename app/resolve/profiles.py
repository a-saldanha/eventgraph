"""Aggregate (chunk, local_entity) mentions + Phase-2 identity clusters into profiles.

A Profile is a compact summary of what we know about one candidate real-world
entity: which surfaces it appeared under, which identifiers (emails/phones)
are attached, which items it occurs in, and a few sample text lines.  Profiles
are the input to blocking + LLM resolution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import defaultdict

from ..graph_model import EntityType
from ..llm.extract import ChunkExtraction, MentionExtraction
from ..resolve.identity import IdentityCluster


@dataclass
class Profile:
    """One candidate real-world entity, assembled from extraction output."""

    id: str                          # stable key, e.g. "person:0"
    type: str                        # "person" | "org" | "location" | "event" | ...
    surfaces: dict[str, int]         # surface -> mention count
    identifiers: set[str]            # emails + phones
    clues: dict[str, set[str]]       # clue_key -> set of values seen (affiliation, role…)
    channels: set[str]               # source_type values (email, whatsapp, …)
    item_ids: set[str]               # real item ids this profile occurs in
    sample_lines: list[str]          # ≤3 representative text snippets
    co_profile_ids: set[str]         # profiles that often co-occur (same item)

    @property
    def canonical_surface(self) -> str:
        """The surface with the highest count, longest on tie."""
        if not self.surfaces:
            return ""
        return max(self.surfaces, key=lambda s: (self.surfaces[s], len(s)))

    @property
    def all_surfaces(self) -> list[str]:
        return sorted(self.surfaces)


def _norm_type(t: str) -> str:
    """Map LLM mention types to a handful of canonical type strings."""
    mapping = {
        "person": "person",
        "org": "org",
        "location": "location",
        "event": "event",
        "money": "money",
        "document": "document",
        "unknown": "unknown",
    }
    return mapping.get(t.lower(), "unknown")


def build_profiles_from_extractions(
    extractions: list[ChunkExtraction],
    items_by_id: dict | None = None,
) -> list[Profile]:
    """Build profiles from LLM chunk extractions.

    Groups mentions by (conversation_id, local_entity) first — within a chunk
    a local_entity is already co-referent.  Across chunks in the same
    conversation we keep local_entity keys as distinct profiles (blocking will
    merge them if they match).
    """
    # (conversation_id, local_entity) -> list of MentionExtraction
    groups: dict[tuple[str, str], list[MentionExtraction]] = defaultdict(list)
    group_meta: dict[tuple[str, str], str] = {}  # -> type

    for ce in extractions:
        for mn in ce.mentions:
            key = (ce.conversation_id, mn.local_entity)
            groups[key].append(mn)
            group_meta[key] = mn.type

    # item_id -> source_type (for channel labelling)
    items_by_id = items_by_id or {}

    # Map: profile key -> Profile
    profiles: dict[str, Profile] = {}
    # item_id -> set of profile keys (for co-occurrence)
    item_to_profiles: dict[str, set[str]] = defaultdict(set)

    for k, (key, mentions) in enumerate(sorted(groups.items())):
        conv_id, local_ent = key
        ptype = _norm_type(group_meta[key])
        pid = f"{ptype}:{k}"

        surfaces: dict[str, int] = defaultdict(int)
        identifiers: set[str] = set()
        clues: dict[str, set[str]] = defaultdict(set)
        item_ids: set[str] = set()
        sample_lines: list[str] = []

        for mn in mentions:
            surfaces[mn.surface] += 1
            item_ids.add(mn.real_item_id)
            for ck, cv in (mn.clues or {}).items():
                if cv:
                    if ck in ("email", "phone"):
                        identifiers.add(str(cv).strip().lower())
                    else:
                        clues[ck].add(str(cv))
            if len(sample_lines) < 3 and mn.surface:
                sample_lines.append(mn.surface)

        # Channel annotation
        channels: set[str] = set()
        for iid in item_ids:
            it = items_by_id.get(iid)
            if it:
                channels.add(it.source_type.value)

        profile = Profile(
            id=pid,
            type=ptype,
            surfaces=dict(surfaces),
            identifiers=identifiers,
            clues={k: v for k, v in clues.items()},
            channels=channels,
            item_ids=item_ids,
            sample_lines=sample_lines[:3],
            co_profile_ids=set(),
        )
        profiles[pid] = profile
        for iid in item_ids:
            item_to_profiles[iid].add(pid)

    # Fill co-occurrence
    for iid, pids in item_to_profiles.items():
        pid_list = sorted(pids)
        for pid in pid_list:
            profiles[pid].co_profile_ids |= (pids - {pid})

    return list(profiles.values())


def build_profiles_from_identity_clusters(
    clusters: list[IdentityCluster],
    items_by_id: dict | None = None,
) -> list[Profile]:
    """Build person profiles from Phase-2 identity clusters (deterministic path)."""
    items_by_id = items_by_id or {}
    profiles: list[Profile] = []

    for k, cl in enumerate(clusters):
        surfaces: dict[str, int] = defaultdict(int)
        item_ids: set[str] = set()
        channels: set[str] = set()

        for item_id, _pi in cl.members:
            it = items_by_id.get(item_id)
            if it:
                channels.add(it.source_type.value)
                item_ids.add(item_id)

        for name in cl.display_names:
            surfaces[name] = surfaces.get(name, 0) + 1
        for ident in cl.identifiers:
            surfaces[ident] = surfaces.get(ident, 0) + 1

        profiles.append(Profile(
            id=f"person:{k}",
            type="person",
            surfaces=dict(surfaces),
            identifiers=set(cl.identifiers),
            clues={},
            channels=channels,
            item_ids=item_ids,
            sample_lines=list(cl.display_names)[:3],
            co_profile_ids=set(),
        ))

    return profiles


def profile_to_dict(p: Profile) -> dict:
    """Serialize a Profile to a plain dict for the LLM prompt."""
    return {
        "id": p.id,
        "type": p.type,
        "surfaces": p.surfaces,
        "identifiers": sorted(p.identifiers),
        "clues": {k: sorted(v) for k, v in p.clues.items()},
        "channels": sorted(p.channels),
        "item_count": len(p.item_ids),
        "sample_lines": p.sample_lines,
    }
