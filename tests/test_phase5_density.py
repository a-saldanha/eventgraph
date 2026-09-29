"""Phase 5: LLM people-merging + wiring relations into edges.

Tests use invented data and FakeLLM — no real Anthropic calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.graph_model import Entity, EntityType, Mention
from app.pipeline.build import (
    _apply_profile_merges_to_people,
    _relations_from_llm,
)
from app.resolve.profiles import Profile
from app.llm.extract import ChunkExtraction, RelationExtraction, MentionExtraction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entity(eid, label, aliases=None, emails=None, owner=False) -> Entity:
    attrs = {"emails": emails or []}
    if owner:
        attrs["owner"] = True
    mentions = [Mention(item_id=f"item_{eid}", text=label)]
    return Entity(
        id=eid,
        type=EntityType.PERSON,
        label=label,
        aliases=aliases or [label],
        mentions=mentions,
        attrs=attrs,
    )


def _profile(pid, surfaces, identifiers=None, item_ids=None) -> Profile:
    surfaces_dict = surfaces if isinstance(surfaces, dict) else {s: 1 for s in surfaces}
    return Profile(
        id=pid,
        type="person",
        surfaces=surfaces_dict,
        identifiers=set(identifiers or []),
        clues={},
        channels={"email"},
        item_ids=set(item_ids or [f"item_{pid}"]),
        sample_lines=list(surfaces_dict.keys())[:3],
        co_profile_ids=set(),
    )


def _chunk(conv_id, relations=None, mentions=None) -> ChunkExtraction:
    ce = ChunkExtraction(
        chunk_id=f"chunk_{conv_id}",
        conversation_id=conv_id,
        relations=relations or [],
        mentions=mentions or [],
    )
    return ce


def _rel(subj_le, obj_le, rtype, item_id="item1") -> RelationExtraction:
    return RelationExtraction(
        type=rtype,
        subject_local_entity=subj_le,
        object_local_entity=obj_le,
        real_item_id=item_id,
        evidence_span="test evidence",
    )


# ===========================================================================
# Test (a): two person profiles that are surface variants merge into one entity
# ===========================================================================

def test_surface_variant_profiles_merge_into_one_person_entity():
    """'Jon Smith' (email profile) and 'Jonathan Smith' (whatsapp name profile)
    should merge into one person entity when the LLM decides they are the same.
    """
    # Two heuristic person entities (from resolve_people identifier-only)
    p_jon = _entity("person:0", "Jon Smith", aliases=["Jon Smith", "jon@work.com"],
                    emails=["jon@work.com"])
    p_jonathan = _entity("person:1", "Jonathan Smith",
                         aliases=["Jonathan Smith"], emails=[])

    # Two extraction profiles (from LLM mentions)
    ep1 = _profile("person:10", ["Jon Smith"], identifiers=["jon@work.com"],
                   item_ids=["item1"])
    ep2 = _profile("person:11", ["Jonathan Smith"], identifiers=[],
                   item_ids=["item2"])

    # LLM says ep1 and ep2 are the same person
    merged_groups = {
        "person:10": {"person:10", "person:11"},
    }

    result, _ = _apply_profile_merges_to_people(
        [p_jon, p_jonathan], [ep1, ep2], merged_groups, owner_label=None,
    )

    # Two heuristic entities should collapse to one
    assert len(result) == 1, f"Expected 1 entity after merge, got {len(result)}: {[e.label for e in result]}"
    merged = result[0]
    assert "Jon Smith" in merged.aliases or "Jonathan Smith" in merged.aliases, \
        f"Merged entity should carry original surfaces: {merged.aliases}"


# ===========================================================================
# Test (b): relation whose endpoints resolve → edge; non-resolving endpoint → skipped
# ===========================================================================

def test_llm_relation_with_resolved_endpoints_becomes_edge():
    """A relation whose both endpoints resolve to entity ids should produce an Edge.
    A relation where one endpoint fails to resolve should be silently skipped.
    """
    # resolve_local: maps (conv_id, local_entity) -> entity_id
    resolve_map = {
        ("conv1", "alice"): "person:0",
        ("conv1", "acme_corp"): "org:0",
        # "unknown_entity" is NOT in the map
    }

    def resolve_local(conv_id, local_entity):
        return resolve_map.get((conv_id, local_entity))

    # One relation that resolves, one that doesn't
    good_rel = _rel("alice", "acme_corp", "works_at", item_id="item1")
    bad_rel  = _rel("alice", "unknown_entity", "paid", item_id="item2")

    ce = _chunk("conv1", relations=[good_rel, bad_rel])

    edges = _relations_from_llm([ce], resolve_local, participant_to_entity={})

    assert len(edges) == 1, f"Expected 1 edge (one skipped), got {len(edges)}: {edges}"
    e = edges[0]
    assert e.source == "person:0"
    assert e.target == "org:0"
    assert e.kind == "works_at"
    assert "item1" in e.evidence_item_ids


def test_llm_relation_unresolvable_both_endpoints_skipped():
    """If neither endpoint resolves, no edge is emitted."""
    def resolve_local(conv_id, local_entity):
        return None  # nothing resolves

    rel = _rel("ghost_a", "ghost_b", "located_in", item_id="item3")
    ce = _chunk("conv2", relations=[rel])
    edges = _relations_from_llm([ce], resolve_local, participant_to_entity={})

    assert edges == [], f"Expected no edges, got: {edges}"


# ===========================================================================
# Test (c): owner stays a single entity after people merging
# ===========================================================================

def test_owner_stays_single_entity_after_profile_merge():
    """Even when the owner appears under multiple surface variants, after profile
    merging the owner should still be exactly ONE entity with owner=True.
    """
    # Owner has two heuristic entities (e.g. email channel + whatsapp leet handle)
    # that were already unified by heuristic resolve_people
    owner_entity = _entity(
        "person:0", "Alice Owner",
        aliases=["Alice Owner", "alice@personal.com", "Al1c3"],
        emails=["alice@personal.com"],
        owner=True,
    )
    # A completely separate person
    other_entity = _entity(
        "person:1", "Bob Other",
        aliases=["Bob Other"],
        emails=["bob@work.com"],
    )

    # Profile for owner surface variant
    ep_owner = _profile("person:20", ["Alice Owner", "Al1c3"],
                        identifiers=["alice@personal.com"], item_ids=["item_a"])
    # Profile for owner's other surface
    ep_owner2 = _profile("person:21", ["Al1c3"],
                         identifiers=[], item_ids=["item_b"])
    # Profile for other person
    ep_other = _profile("person:22", ["Bob Other"],
                        identifiers=["bob@work.com"], item_ids=["item_c"])

    # LLM merges ep_owner and ep_owner2 into one group
    merged_groups = {
        "person:20": {"person:20", "person:21"},
        "person:22": {"person:22"},
    }

    result, _ = _apply_profile_merges_to_people(
        [owner_entity, other_entity],
        [ep_owner, ep_owner2, ep_other],
        merged_groups,
        owner_label="Alice Owner",
    )

    # Owner should remain exactly one entity
    owner_entities = [e for e in result if e.attrs.get("owner")]
    assert len(owner_entities) == 1, \
        f"Expected exactly 1 owner entity, got {len(owner_entities)}: {[e.label for e in owner_entities]}"
    assert owner_entities[0].label == "Alice Owner"

    # Total: 2 entities (owner + other)
    assert len(result) == 2, \
        f"Expected 2 total entities, got {len(result)}: {[e.label for e in result]}"
