"""Deterministic identity must-links: same email or phone means the same person.

Runs before any LLM. Two participants with an identical normalized identifier are
union-linked with recorded evidence. Role and shared mailboxes are excluded, so
several people sending from support@example.com are never merged into one identity.
Different identifiers of one person (a second email, a nickname) are *not* linked
here — that judgment belongs to the resolution phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schema import SourceItem

# participant kinds that never anchor an identity (not a single private person)
_NON_IDENTITY_KINDS = {"placeholder", "group", "system", "org"}

Ref = tuple[str, int]  # (item_id, participant_index)


@dataclass
class IdentityCluster:
    key: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    display_names: set[str] = field(default_factory=set)
    members: list[Ref] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def identifiers(self) -> set[str]:
        return self.emails | self.phones


@dataclass
class IdentityIndex:
    clusters: list[IdentityCluster]
    by_ref: dict[Ref, str]            # (item_id, p_index) -> cluster key
    by_identifier: dict[str, str]     # normalized email/phone -> cluster key

    def cluster(self, key: str) -> IdentityCluster | None:
        return next((c for c in self.clusters if c.key == key), None)


def build_identity_index(items: list[SourceItem]) -> IdentityIndex:
    """Group participants that share a normalized email or phone into identities."""
    # Each distinct identifier string is its own group; identical identifiers collapse
    # naturally. We record which (item, participant) refs and names sit on each.
    groups: dict[str, IdentityCluster] = {}
    by_ref: dict[Ref, str] = {}
    for it in items:
        for pi, p in enumerate(it.participants):
            if p.kind in _NON_IDENTITY_KINDS:
                continue
            if p.id_type not in ("email", "phone") or not p.id_value:
                continue
            ident = p.id_value.strip().lower() if p.id_type == "email" else p.id_value
            key = f"{p.id_type}:{ident}"
            cl = groups.get(key)
            if cl is None:
                cl = IdentityCluster(key=key)
                cl.evidence.append(f"shared {p.id_type} {ident}")
                groups[key] = cl
            (cl.emails if p.id_type == "email" else cl.phones).add(ident)
            if p.display_name:
                cl.display_names.add(p.display_name)
            ref = (it.id, pi)
            cl.members.append(ref)
            by_ref[ref] = key

    clusters = list(groups.values())
    by_identifier = {ident: c.key for c in clusters for ident in c.identifiers}
    return IdentityIndex(clusters=clusters, by_ref=by_ref, by_identifier=by_identifier)
