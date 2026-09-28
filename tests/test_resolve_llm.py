"""Phase 4: blocking + LLM resolution tests. All LLM calls use FakeLLM (no network).

Invented data only — no archive-specific strings.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.resolve.profiles import Profile
from app.resolve.blocking import build_blocks
from app.resolve.llm_resolve import resolve_profiles, ClusterResult


# ---------------------------------------------------------------------------
# FakeLLM: returns a fixed JSON response keyed by the profiles in the block.
# ---------------------------------------------------------------------------

class FakeLLM:
    """Offline stand-in: returns pre-baked JSON responses or a default."""

    def __init__(self, responses: list[dict] | None = None):
        # Each entry in `responses` is the raw dict we want the LLM to return.
        # Calls are consumed in order (first call → first response, etc.).
        self._responses = list(responses or [])
        self._call_count = 0

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        return self._next_response()

    def complete_tool(self, system: str, user: str, tool: dict, max_tokens: int = 4096) -> str:
        return self._next_response()

    def _next_response(self) -> str:
        if self._responses:
            resp = self._responses[self._call_count % len(self._responses)]
            self._call_count += 1
            return json.dumps(resp)
        # Default: each profile in its own singleton cluster.
        self._call_count += 1
        return json.dumps({"clusters": [], "derived_relations": []})


# ---------------------------------------------------------------------------
# Helpers to build test profiles
# ---------------------------------------------------------------------------

def _profile(pid, ptype, surfaces, identifiers=None, channels=None, item_ids=None,
             clues=None, sample_lines=None) -> Profile:
    surfaces_dict = surfaces if isinstance(surfaces, dict) else {s: 1 for s in surfaces}
    return Profile(
        id=pid,
        type=ptype,
        surfaces=surfaces_dict,
        identifiers=set(identifiers or []),
        clues=clues or {},
        channels=set(channels or ["email"]),
        item_ids=set(item_ids or [f"item_{pid}"]),
        sample_lines=sample_lines or list(surfaces_dict.keys())[:3],
        co_profile_ids=set(),
    )


# ===========================================================================
# Test 1: leet handle + full name merge on structural evidence
# ===========================================================================

def test_leet_handle_and_fullname_merge():
    """A leet handle (J@n3) and a full name (Jane Doe) sharing a conversation
    should merge when the LLM returns high-confidence evidence.
    """
    p1 = _profile("person:0", "person", ["J@n3"], item_ids=["m1"])
    p2 = _profile("person:1", "person", ["Jane Doe"], item_ids=["m1"])

    fake_llm = FakeLLM([{
        "clusters": [
            {
                "member_ids": ["person:0", "person:1"],
                "canonical_name": "Jane Doe",
                "confidence": "high",
                "evidence": "Same conversation m1; J@n3 is a leet handle matching Jane.",
            }
        ],
        "derived_relations": [],
    }])

    bundle = resolve_profiles([p1, p2], "person", client=fake_llm, use_cache=False)

    # The two profiles should be merged into one group.
    merged = {root: members for root, members in bundle.merged_groups.items()
               if len(members) > 1}
    assert merged, "Expected at least one merged group"
    # MergeRecord should be emitted.
    assert len(bundle.merges) >= 1
    assert any("Jane Doe" in m.merged_forms or "J@n3" in m.merged_forms
               for m in bundle.merges)


# ===========================================================================
# Test 2: same first name / different surname stay apart
# ===========================================================================

def test_same_firstname_different_surname_stay_apart():
    """Jane Smith and Jane Doe share only a first name — they must NOT merge."""
    p1 = _profile("person:0", "person", ["Jane Smith"], item_ids=["m1"])
    p2 = _profile("person:1", "person", ["Jane Doe"], item_ids=["m2"])

    # LLM correctly keeps them separate (two singleton clusters).
    fake_llm = FakeLLM([{
        "clusters": [
            {"member_ids": ["person:0"], "canonical_name": "Jane Smith",
             "confidence": "high", "evidence": "Distinct surname."},
            {"member_ids": ["person:1"], "canonical_name": "Jane Doe",
             "confidence": "high", "evidence": "Distinct surname."},
        ],
        "derived_relations": [],
    }])

    bundle = resolve_profiles([p1, p2], "person", client=fake_llm, use_cache=False)

    merged = {root: members for root, members in bundle.merged_groups.items()
               if len(members) > 1}
    assert not merged, f"Unexpected merge: {merged}"
    assert len(bundle.merges) == 0


# ===========================================================================
# Test 3: invented canonical name is rejected
# ===========================================================================

def test_invented_canonical_name_rejected():
    """The LLM invents a canonical name not present in any member surface → drop cluster."""
    p1 = _profile("person:0", "person", ["Alice"], item_ids=["m1"])
    p2 = _profile("person:1", "person", ["Bob"], item_ids=["m2"])

    fake_llm = FakeLLM([{
        "clusters": [
            {
                "member_ids": ["person:0", "person:1"],
                "canonical_name": "Completely Invented Name",  # not in any surface
                "confidence": "high",
                "evidence": "They are the same.",
            }
        ],
        "derived_relations": [],
    }])

    bundle = resolve_profiles([p1, p2], "person", client=fake_llm, use_cache=False)

    # Invalid cluster should be dropped; no merge should occur.
    merged = {root: members for root, members in bundle.merged_groups.items()
               if len(members) > 1}
    assert not merged, f"Merge should have been rejected: {merged}"


# ===========================================================================
# Test 4: low confidence → review queue
# ===========================================================================

def test_low_confidence_goes_to_review_queue():
    """A low-confidence merge should go to review_queue, not merged_groups."""
    p1 = _profile("person:0", "person", ["Alex Jones"], item_ids=["m1"])
    p2 = _profile("person:1", "person", ["Alex J."], item_ids=["m2"])

    fake_llm = FakeLLM([{
        "clusters": [
            {
                "member_ids": ["person:0", "person:1"],
                "canonical_name": "Alex Jones",
                "confidence": "low",
                "evidence": "Similar name, no other evidence.",
            }
        ],
        "derived_relations": [],
    }])

    bundle = resolve_profiles([p1, p2], "person", client=fake_llm, use_cache=False)

    # Low-confidence merge goes to review queue, not merged.
    assert len(bundle.review_queue) >= 1, "Expected items in review queue"
    merged = {root: members for root, members in bundle.merged_groups.items()
               if len(members) > 1}
    assert not merged, "Low-confidence merge should not be applied"


# ===========================================================================
# Test 5: event vs organizer stay separate
# ===========================================================================

def test_event_vs_organizer_separate():
    """An event and its organizing org have different types → cannot-link."""
    p_event = _profile("event:0", "event", ["Tech Summit 2024"], item_ids=["m1"])
    p_org = _profile("org:0", "org", ["Tech Corp"], item_ids=["m1"])

    # Both types present; blocking should keep them in separate type-groups.
    blocks = build_blocks([p_event, p_org])
    # They should be in different blocks because they have different types.
    event_block = [b for b in blocks if any(p.type == "event" for p in b)]
    org_block = [b for b in blocks if any(p.type == "org" for p in b)]
    # No block should mix event and org.
    for block in blocks:
        types = {p.type for p in block}
        assert len(types) == 1, f"Block mixes types: {types}"


# ===========================================================================
# Test 6: "City, Country" merges with "City"
# ===========================================================================

def test_city_country_merges_with_city():
    """'Springfield, USA' and 'Springfield' are the same location → should merge."""
    p1 = _profile("loc:0", "location", ["Springfield, USA"], item_ids=["m1"])
    p2 = _profile("loc:1", "location", ["Springfield"], item_ids=["m2"])

    # Blocking by containment: "springfield" is in "springfield, usa".
    blocks = build_blocks([p1, p2])
    # They should end up in the same block.
    assert any(len(b) == 2 for b in blocks), \
        f"Springfield variants should be co-blocked; got blocks: {[[p.id for p in b] for b in blocks]}"

    # LLM confirms the merge.
    fake_llm = FakeLLM([{
        "clusters": [
            {
                "member_ids": ["loc:0", "loc:1"],
                "canonical_name": "Springfield",
                "confidence": "medium",
                "evidence": "'Springfield, USA' contains 'Springfield'.",
            }
        ],
        "derived_relations": [],
    }])

    bundle = resolve_profiles([p1, p2], "location", client=fake_llm, use_cache=False)
    merged = {root: members for root, members in bundle.merged_groups.items()
               if len(members) > 1}
    assert merged, "City,Country and City should merge"


# ===========================================================================
# Test 7: street address links to city via derived_relation
# ===========================================================================

def test_street_address_links_to_city():
    """A street address is located_in its city — this should appear as a derived_relation,
    not a merge (they are different entities at different granularities)."""
    p_street = _profile("loc:0", "location", ["42 Elm Street"], item_ids=["m1"])
    p_city = _profile("loc:1", "location", ["Shelbyville"], item_ids=["m1"])

    fake_llm = FakeLLM([{
        "clusters": [
            {"member_ids": ["loc:0"], "canonical_name": "42 Elm Street",
             "confidence": "high", "evidence": "Unique street address."},
            {"member_ids": ["loc:1"], "canonical_name": "Shelbyville",
             "confidence": "high", "evidence": "City name."},
        ],
        "derived_relations": [
            {
                "subject_id": "loc:0",
                "object_id": "loc:1",
                "type": "located_in",
                "evidence": "42 Elm Street is in Shelbyville.",
            }
        ],
    }])

    bundle = resolve_profiles([p_street, p_city], "location", client=fake_llm, use_cache=False)

    # No merge.
    merged = {root: members for root, members in bundle.merged_groups.items()
               if len(members) > 1}
    assert not merged, "Street address should not merge with city"

    # But a derived relation is emitted.
    rels = bundle.derived_relations
    assert any(r.type == "located_in" for r in rels), \
        f"Expected located_in derived_relation, got: {rels}"
