"""Graph-first retrieval and traversal for query answering.

Every query starts by locating seed entities in the graph, then BFS-expanding
their neighbourhood to find related entities, edges and evidence items.
The LLM (or deterministic logic) then answers *from that subgraph* rather than
from a flat keyword-match over all items.

Public surface:
  nav = GraphNavigator(bundle)
  nav.match_entities(text)          -> list[Entity]
  nav.neighborhood(seed_ids, depth) -> (entities, edges, item_ids)
  nav.money_rows()                  -> list[dict]   (for the money query)
  nav.who_rows()                    -> list[dict]   (for the who query)
  nav.timeline_episodes()           -> list[dict]   (for the timeline query)
  nav.ask_context(question, k)      -> (subgraph_text, items)
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline.build import Bundle
    from .graph_model import Edge, Entity

_STOP = frozenset(
    "the a an of to in on for and or is are was were be been with at by from "
    "as it this that these those i you he she they we my your our what when "
    "where who how did do does me about into out over under".split()
)

_ROLE_LOCAL = frozenset(
    "noreply no-reply donotreply notifications support help info contact "
    "team hello hi admin postmaster mailer-daemon listserv bounce "
    "newsletter updates alerts news sales marketing".split()
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _norm(text).split() if t not in _STOP and len(t) > 1}


class GraphNavigator:
    """Walk the entity graph for query answering."""

    def __init__(self, bundle: "Bundle") -> None:
        self.bundle = bundle
        self.graph = bundle.graph
        self._entity: dict[str, "Entity"] = {e.id: e for e in self.graph.entities}
        self._item: dict = {it.id: it for it in bundle.items}

        # Adjacency: entity_id -> [(neighbour_id, edge)]
        self._adj: dict[str, list[tuple[str, "Edge"]]] = defaultdict(list)
        for ed in self.graph.edges:
            self._adj[ed.source].append((ed.target, ed))
            self._adj[ed.target].append((ed.source, ed))

        # Surface token index: frozenset(tokens) of label/alias -> [entity_id]
        self._tok_index: dict[str, list[str]] = defaultdict(list)
        for e in self.graph.entities:
            for surface in [e.label] + list(e.aliases):
                for tok in _tokens(surface):
                    self._tok_index[tok].append(e.id)

    # ── entity matching ──────────────────────────────────────────────────────

    def match_entities(self, text: str, top_k: int = 6) -> list["Entity"]:
        """Return entities whose labels/aliases share the most tokens with `text`."""
        q = _tokens(text)
        if not q:
            return []
        scores: dict[str, int] = {}
        for tok in q:
            for eid in self._tok_index.get(tok, []):
                scores[eid] = scores.get(eid, 0) + 1
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [self._entity[eid] for eid, _ in ranked if eid in self._entity]

    # ── neighbourhood BFS ────────────────────────────────────────────────────

    def neighborhood(
        self,
        seed_ids: set[str],
        depth: int = 2,
    ) -> tuple[list["Entity"], list["Edge"], set[str]]:
        """BFS from seeds. Returns (entities, edges, evidence_item_ids)."""
        visited = set(seed_ids) & set(self._entity)
        frontier = set(visited)
        seen_edges: set[str] = set()
        item_ids: set[str] = set()

        for _ in range(depth):
            next_frontier: set[str] = set()
            for eid in frontier:
                for nbr_id, ed in self._adj.get(eid, []):
                    if ed.id not in seen_edges:
                        seen_edges.add(ed.id)
                        item_ids.update(ed.evidence_item_ids)
                    if nbr_id not in visited:
                        visited.add(nbr_id)
                        next_frontier.add(nbr_id)
            frontier = next_frontier

        for eid in visited:
            e = self._entity.get(eid)
            if e:
                item_ids.update(e.item_ids)

        entities = [self._entity[eid] for eid in visited]
        edges = [ed for ed in self.graph.edges if ed.id in seen_edges]
        return entities, edges, item_ids

    # ── money traversal ──────────────────────────────────────────────────────

    def money_rows(self) -> list[dict]:
        """All money entities with payer/payee pulled from the graph."""
        rows = []
        for e in self.graph.entities:
            if e.type.value != "money":
                continue
            payers = [
                self._entity[ed.source].label
                for ed in self.graph.edges
                if ed.target == e.id and ed.kind == "paid"
                and ed.source in self._entity
            ]
            payees = [
                self._entity[ed.target].label
                for ed in self.graph.edges
                if ed.source == e.id and ed.kind in ("paid_to", "paid")
                and ed.target in self._entity
            ]
            rows.append({
                "entity_id": e.id,
                "amount": e.attrs.get("amount", 0),
                "currency": e.attrs.get("currency"),
                "label": e.label,
                "payers": payers[:3],
                "payees": payees[:3],
                "needs_review": bool(e.attrs.get("needs_review")),
                "flagged": not e.attrs.get("currency"),
                "item_ids": sorted(e.item_ids)[:8],
                "context": (e.attrs.get("snippets") or [""])[0][:80],
            })
        return sorted(rows, key=lambda r: -r["amount"])

    def money_totals(self) -> dict[str, float]:
        """Sum resolved amounts per currency. Excludes flagged."""
        totals: dict[str, float] = {}
        for r in self.money_rows():
            if not r["flagged"] and r["currency"]:
                totals[r["currency"]] = totals.get(r["currency"], 0.0) + r["amount"]
        return totals

    # ── who traversal ────────────────────────────────────────────────────────

    def _is_system(self, e: "Entity") -> bool:
        if e.type.value != "person":
            return False
        for em in e.attrs.get("emails", []):
            local = em.split("@")[0].lower().replace(".", "").replace("+", "")
            if local in _ROLE_LOCAL:
                return True
        return False

    def who_rows(self, top: int = 15) -> list[dict]:
        """People ranked by graph degree (correspondence weight), excluding owner/systems."""
        degree: dict[str, float] = {}
        for ed in self.graph.edges:
            if ed.kind == "corresponded_with":
                degree[ed.source] = degree.get(ed.source, 0.0) + ed.weight
                degree[ed.target] = degree.get(ed.target, 0.0) + ed.weight

        people = [
            e for e in self.graph.entities
            if e.type.value == "person"
            and not e.attrs.get("owner")
            and not self._is_system(e)
        ]
        people.sort(key=lambda e: -(degree.get(e.id, 0.0) + len(e.mentions) * 0.1))

        rows = []
        for e in people[:top]:
            orgs = [
                self._entity[ed.target].label
                for ed in self.graph.edges
                if ed.source == e.id and ed.kind == "affiliated_with"
                and ed.target in self._entity
            ]
            # Derive channels from item source_types
            channels = list({
                getattr(self._item.get(iid), "source_type", type("_", (), {"value": "?"})()).value
                for iid in list(e.item_ids)[:20]
            })
            rows.append({
                "entity_id": e.id,
                "name": e.label,
                "emails": e.attrs.get("emails", [])[:2],
                "mentions": len(e.mentions),
                "correspondence_weight": degree.get(e.id, 0.0),
                "orgs": orgs[:3],
                "channels": [c for c in channels if c != "?"][:4],
                "item_ids": sorted(list(e.item_ids))[:6],
            })
        return rows

    # ── timeline traversal ───────────────────────────────────────────────────

    def timeline_episodes(self) -> list[dict]:
        """Topic-based episodes (LLM mode) or heuristic subevent grouping."""
        # LLM mode: use topics stored in relevance verdicts
        topic_items: dict[str, list] = defaultdict(list)
        for verdict in self.graph.relevance:
            it = self._item.get(verdict.item_id)
            if not it or not getattr(it, "timestamp", None):
                continue
            for topic in (verdict.topics or []):
                topic_items[topic].append(it)

        episodes: list[dict] = []

        if topic_items:
            gap_days = 14
            for topic, items in topic_items.items():
                items_s = sorted(items, key=lambda it: it.timestamp)
                current = [items_s[0]]
                sub_eps = []
                for it in items_s[1:]:
                    if (it.timestamp - current[-1].timestamp).days > gap_days:
                        sub_eps.append(current)
                        current = [it]
                    else:
                        current.append(it)
                sub_eps.append(current)
                for ep in sub_eps:
                    episodes.append({
                        "label": topic,
                        "start": ep[0].timestamp.isoformat(),
                        "end": ep[-1].timestamp.isoformat(),
                        "count": len(ep),
                        "item_ids": [it.id for it in ep[:5]],
                        "source": "topic",
                    })
        else:
            # Heuristic: use pre-built bundle timeline (subevent entities)
            for r in self.bundle.timeline:
                episodes.append({
                    "label": r["label"],
                    "start": r["start"],
                    "end": r["end"],
                    "count": r["count"],
                    "entity_id": r.get("entity_id"),
                    "item_ids": [],
                    "source": "heuristic",
                })

        return sorted(episodes, key=lambda e: e.get("start", ""))

    # ── ask context builder ──────────────────────────────────────────────────

    def ask_context(
        self,
        question: str,
        k_items: int = 16,
    ) -> tuple[str, list]:
        """Build graph-navigation context for an /ask query.

        Returns (subgraph_text, top_items) where subgraph_text is a compact
        representation of the neighbourhood around entities matched in `question`.
        """
        # 1. Match entities from the question
        seeds = self.match_entities(question, top_k=5)
        seed_ids = {e.id for e in seeds}

        # 2. If no entity match, fall back to money/people seed set
        if not seed_ids:
            seed_ids = {
                e.id for e in self.graph.entities
                if e.type.value in ("money", "person") and not e.attrs.get("owner")
            }

        # 3. BFS expand (depth 2)
        nbr_entities, nbr_edges, nbr_item_ids = self.neighborhood(seed_ids, depth=2)

        # 4. Score items: prefer those in neighbourhood + token overlap with question
        q_tok = _tokens(question)
        scored: list[tuple[float, object]] = []
        for it in self.bundle.items:
            in_nbr = it.id in nbr_item_ids
            overlap = len(q_tok & _tokens(
                f"{getattr(it, 'subject', '') or ''} {it.body} "
                f"{getattr(it, 'sender_display', '') or ''}"
            ))
            score = (2.0 if in_nbr else 0.0) + overlap
            if score > 0:
                scored.append((score, it))
        scored.sort(key=lambda x: -x[0])
        top_items = [it for _, it in scored[:k_items]]
        if not top_items:
            top_items = list(self.bundle.items)[:k_items]

        # 5. Build compact subgraph description
        lines = []
        # Seed entities (most relevant)
        if seeds:
            lines.append("MATCHED ENTITIES:")
            for e in seeds[:5]:
                suffix = ""
                if e.type.value == "person":
                    emails = e.attrs.get("emails", [])
                    suffix = f" <{', '.join(emails[:2])}>" if emails else ""
                elif e.type.value == "money":
                    suffix = f" = {e.label}"
                lines.append(f"  {e.type.value.upper()} {e.label}{suffix}")

        # Key edges in neighbourhood
        edge_lines = []
        for ed in nbr_edges[:20]:
            src = self._entity.get(ed.source)
            tgt = self._entity.get(ed.target)
            if src and tgt:
                edge_lines.append(f"  {src.label} —[{ed.kind}]→ {tgt.label}")
        if edge_lines:
            lines.append("RELATIONS:")
            lines.extend(edge_lines)

        # Other entities in neighbourhood (not seeds)
        other = [e for e in nbr_entities if e.id not in seed_ids][:12]
        if other:
            lines.append("RELATED ENTITIES:")
            for e in other:
                lines.append(f"  {e.type.value.upper()} {e.label}")

        subgraph_text = "\n".join(lines) if lines else "(no matching entities)"
        return subgraph_text, top_items
