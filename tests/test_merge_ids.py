"""Phase 1 tests: stable content-derived IDs and conservative name merge.

All fixtures use invented data; no network calls are made.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.graph_model import Edge, Entity, EntityType, Mention
from app.schema import SourceItem, SourceType, Provenance


def _prov():
    return Provenance(batch_file="test.md", item_index=0)


def _item(iid="i0", body="hello"):
    return SourceItem(
        id=iid, source_type=SourceType.EMAIL, body=body,
        channel="email", timestamp=datetime(2024, 1, 1), provenance=_prov(),
    )


def _person(eid, label, aliases=None, emails=None, item_ids=None):
    item_ids = item_ids or ["i0"]
    return Entity(
        id=eid, type=EntityType.PERSON, label=label,
        aliases=aliases or [],
        mentions=[Mention(item_id=i, text=label) for i in item_ids],
        attrs={"emails": emails or []},
    )


def _edge(src, tgt, kind="corresponded_with"):
    return Edge(id="e0", source=src, target=tgt, kind=kind,
                weight=1.0, evidence_item_ids=["i0"])


# ── stable IDs: same key always produces same ID ──────────────────────────────

def test_stable_id_same_email_same_id():
    from app.pipeline.build import _assign_stable_ids
    e1 = _person("person:0", "Alice", emails=["alice@work.com"])
    e2 = _person("person:1", "Alice A.", emails=["alice@work.com"])
    id1 = _assign_stable_ids([e1])["person:0"]
    e2b = _person("person:1", "Alice A.", emails=["alice@work.com"])
    id2 = _assign_stable_ids([e2b])["person:1"]
    assert id1 == id2, "Same email → same stable ID across builds"


def test_stable_id_different_email_different_id():
    from app.pipeline.build import _assign_stable_ids
    e1 = _person("person:0", "Alice", emails=["alice@work.com"])
    e2 = _person("person:1", "Bob", emails=["bob@work.com"])
    m = _assign_stable_ids([e1, e2])
    assert m["person:0"] != m["person:1"]


def test_stable_id_type_prefix_preserved():
    from app.pipeline.build import _assign_stable_ids
    e = _person("person:0", "Alice", emails=["alice@work.com"])
    m = _assign_stable_ids([e])
    assert m["person:0"].startswith("person:")


def test_stable_id_no_collision_for_different_types():
    from app.pipeline.build import _assign_stable_ids
    person = _person("person:0", "Acme")
    org = Entity(id="org:0", type=EntityType.ORG, label="Acme",
                 mentions=[Mention(item_id="i0", text="Acme")])
    m = _assign_stable_ids([person, org])
    assert m["person:0"] != m["org:0"]


# ── remap: edges and merges updated after ID reassignment ─────────────────────

def test_remap_updates_edge_endpoints():
    from app.pipeline.build import _assign_stable_ids, _remap
    e1 = _person("person:0", "Alice", emails=["alice@work.com"])
    e2 = _person("person:1", "Bob", emails=["bob@work.com"])
    ed = _edge("person:0", "person:1")

    old_to_new = _assign_stable_ids([e1, e2])
    _remap(old_to_new, [e1, e2], [ed], [], [])

    assert ed.source == old_to_new["person:0"]
    assert ed.target == old_to_new["person:1"]


# ── conservative name merge: basic merging ───────────────────────────────────

def test_same_full_name_no_conflict_merges():
    from app.pipeline.build import _conservative_name_merge
    e1 = _person("person:0", "Alice Smith",
                 aliases=["Alice Smith"], emails=["alice@foo.com"], item_ids=["i0"])
    e2 = _person("person:1", "Alice Smith",
                 aliases=["Alice Smith"], emails=[], item_ids=["i1"])
    merged, edges, new_merges = _conservative_name_merge([e1, e2], [])
    assert len(merged) == 1, f"Expected 1, got {len(merged)}: {[e.label for e in merged]}"
    assert len(new_merges) == 1


def test_single_token_name_not_merged():
    from app.pipeline.build import _conservative_name_merge
    e1 = _person("person:0", "Alice", aliases=["Alice"], item_ids=["i0"])
    e2 = _person("person:1", "Alice", aliases=["Alice"], item_ids=["i1"])
    merged, _, new_merges = _conservative_name_merge([e1, e2], [])
    # Single-token names must not be merged — too ambiguous
    assert len(merged) == 2, "Single-token names must not be merged"
    assert len(new_merges) == 0


def test_conflicting_emails_same_domain_not_merged():
    from app.pipeline.build import _conservative_name_merge
    # Both are "Alice Smith" but different emails at the same domain
    e1 = _person("person:0", "Alice Smith", emails=["alice1@corp.com"], item_ids=["i0"])
    e2 = _person("person:1", "Alice Smith", emails=["alice2@corp.com"], item_ids=["i1"])
    merged, _, new_merges = _conservative_name_merge([e1, e2], [])
    # Conflicting identifiers at the same domain → different people, no merge
    assert len(merged) == 2
    assert len(new_merges) == 0


def test_different_name_not_merged():
    from app.pipeline.build import _conservative_name_merge
    e1 = _person("person:0", "Alice Smith", item_ids=["i0"])
    e2 = _person("person:1", "Bob Jones", item_ids=["i1"])
    merged, _, _ = _conservative_name_merge([e1, e2], [])
    assert len(merged) == 2


def test_group_mailbox_not_merged():
    from app.pipeline.build import _conservative_name_merge
    # "support" is in _ROLE_LOCAL_PARTS — should not be merged
    e1 = _person("person:0", "Support Team",
                 emails=["support@org.com"], item_ids=["i0"])
    e2 = _person("person:1", "Support Team",
                 emails=[], item_ids=["i1"])
    merged, _, new_merges = _conservative_name_merge([e1, e2], [])
    assert len(merged) == 2, "Group mailbox entities must not be name-merged"
    assert len(new_merges) == 0


def test_merge_remaps_edge_endpoints():
    from app.pipeline.build import _conservative_name_merge
    e1 = _person("person:0", "Alice Smith", item_ids=["i0"])
    e2 = _person("person:1", "Alice Smith", item_ids=["i1"])
    e3 = _person("person:2", "Bob Jones", item_ids=["i0"])
    ed = _edge("person:1", "person:2")  # e2 will be merged into e1 (more mentions)

    merged, out_edges, _ = _conservative_name_merge([e1, e2, e3], [ed])
    canon_id = next(e for e in merged if e.label == "Alice Smith").id
    assert len(merged) == 2  # Alice + Bob
    if out_edges:
        assert out_edges[0].source == canon_id or out_edges[0].target == canon_id


def test_name_merge_deduplicates_edges():
    from app.pipeline.build import _conservative_name_merge
    e1 = _person("person:0", "Alice Smith", item_ids=["i0"])
    e2 = _person("person:1", "Alice Smith", item_ids=["i1"])
    e3 = _person("person:2", "Bob Jones", item_ids=["i0"])
    # Two edges from different Alice entities to Bob — after merge they'd be duplicate
    ed1 = Edge(id="e0", source="person:0", target="person:2",
               kind="corresponded_with", weight=1.0, evidence_item_ids=["i0"])
    ed2 = Edge(id="e1", source="person:1", target="person:2",
               kind="corresponded_with", weight=1.0, evidence_item_ids=["i1"])

    merged, out_edges, _ = _conservative_name_merge([e1, e2, e3], [ed1, ed2])
    # After merge: only one (Alice→Bob, corresponded_with) edge
    assert len(merged) == 2
    assert len(out_edges) == 1
    assert out_edges[0].weight == 2.0  # weights accumulated


# ── G1 fix: _apply_profile_merges_to_people ─────────────────────────────────

def test_g1_no_new_counter_ids_created():
    """After profile merging, all entity IDs must come from the input entity set."""
    from app.pipeline.build import _apply_profile_merges_to_people

    e1 = _person("person:0", "Alice", emails=["alice@work.com"], item_ids=["i0"])
    e2 = _person("person:1", "Alice A.", emails=["alice@work.com"], item_ids=["i1"])

    class _FakeProfile:
        def __init__(self, pid, surfaces, identifiers, item_ids):
            self.id = pid
            self.surfaces = {s: 1 for s in surfaces}
            self.identifiers = set(identifiers)
            self.item_ids = set(item_ids)

    ep1 = _FakeProfile("prof:0", ["Alice"], ["alice@work.com"], ["i0"])
    ep2 = _FakeProfile("prof:1", ["Alice A."], ["alice@work.com"], ["i1"])

    merged_groups = {"prof:0": {"prof:0", "prof:1"}}
    result, old_to_new = _apply_profile_merges_to_people(
        [e1, e2], [ep1, ep2], merged_groups, owner_label=None,
    )

    valid_ids = {"person:0", "person:1"}
    for e in result:
        assert e.id in valid_ids, f"New counter ID created: {e.id}"


def test_g1_owner_preserved_after_merge():
    """Owner flag and label must survive profile-driven merging."""
    from app.pipeline.build import _apply_profile_merges_to_people

    owner = _person("person:0", "Alice Owner",
                    emails=["alice@work.com"], item_ids=["i0"])
    owner.attrs["owner"] = True
    other = _person("person:1", "Alice A.",
                    emails=["alice@work.com"], item_ids=["i1"])

    class _FakeProfile:
        def __init__(self, pid, surfaces, identifiers, item_ids):
            self.id = pid
            self.surfaces = {s: 1 for s in surfaces}
            self.identifiers = set(identifiers)
            self.item_ids = set(item_ids)

    ep1 = _FakeProfile("prof:0", ["Alice Owner"], ["alice@work.com"], ["i0"])
    ep2 = _FakeProfile("prof:1", ["Alice A."], ["alice@work.com"], ["i1"])
    merged_groups = {"prof:0": {"prof:0", "prof:1"}}

    result, _ = _apply_profile_merges_to_people(
        [owner, other], [ep1, ep2], merged_groups, owner_label="Alice Owner",
    )
    owner_entities = [e for e in result if e.attrs.get("owner")]
    assert len(owner_entities) == 1
    assert owner_entities[0].label == "Alice Owner"


# ── build_graph: end-to-end ID stability ─────────────────────────────────────

def test_build_graph_produces_stable_ids_across_runs():
    """Two builds from the same items must produce the same entity IDs."""
    from app.pipeline.build import build_graph
    items = [
        SourceItem(
            id=f"item{k}", source_type=SourceType.EMAIL, body=f"Message {k}",
            channel="email", timestamp=datetime(2024, 1, k + 1),
            provenance=_prov(),
            participants=[],
        )
        for k in range(3)
    ]
    b1 = build_graph(items, mode="heuristic")
    b2 = build_graph(items, mode="heuristic")

    ids1 = sorted(e.id for e in b1.graph.entities)
    ids2 = sorted(e.id for e in b2.graph.entities)
    assert ids1 == ids2, f"IDs differ across runs:\n  run1: {ids1}\n  run2: {ids2}"


def test_build_graph_no_duplicate_entity_ids():
    from app.pipeline.build import build_graph
    from app.schema import Participant
    items = [
        SourceItem(
            id="item0", source_type=SourceType.EMAIL, body="Hello from Alice",
            channel="email", timestamp=datetime(2024, 1, 1), provenance=_prov(),
            participants=[],
        )
    ]
    b = build_graph(items, mode="heuristic")
    ids = [e.id for e in b.graph.entities]
    assert len(ids) == len(set(ids)), f"Duplicate entity IDs: {ids}"
