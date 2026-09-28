"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import GraphView from "@/components/GraphView";
import TimelineView from "@/components/TimelineView";
import CorpusView from "@/components/CorpusView";
import QueriesView from "@/components/QueriesView";
import IngestView from "@/components/IngestView";
import OverviewView from "@/components/OverviewView";
import Drawer from "@/components/Drawer";

const TABS = ["Overview", "Graph", "Timeline", "Corpus", "Queries", "Resolution", "Ingest"];

export default function Page() {
  const [tab, setTab] = useState("Overview");
  const [stats, setStats] = useState(null);
  const [selection, setSelection] = useState(null); // {kind:'entity'|'item', id}
  const [highlight, setHighlight] = useState([]);
  const [reloadKey, setReloadKey] = useState(0);

  const refresh = () => { api.stats().then(setStats).catch(() => {}); setReloadKey((k) => k + 1); };
  useEffect(() => { api.stats().then(setStats).catch(() => {}); }, []);

  const selEntity = (id) => setSelection({ kind: "entity", id });
  const selItem = (id) => setSelection({ kind: "item", id });

  return (
    <div className="app">
      <div className="top">
        <span className="brand">Event Knowledge Graph</span>
        <div className="tabs">
          {TABS.map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>
        <span className="spacer" />
        {stats && (
          <span className="stat">
            {stats.items} items → {stats.relevant_items} relevant · {stats.exact_dupes + stats.near_dupes} dupes ·
            {" "}{stats.entities} entities · {stats.edges} edges · {stats.merges} merges
          </span>
        )}
      </div>

      <div className="main">
        <div className="content" key={reloadKey}>
          {tab === "Overview" && <OverviewView stats={stats} onGo={setTab} />}
          {tab === "Ingest" && <IngestView onIngested={() => { refresh(); setTab("Graph"); }} />}
          {tab === "Graph" && <GraphView onSelectEntity={selEntity} highlight={highlight} />}
          {tab === "Timeline" && <TimelineView onSelectEntity={selEntity} />}
          {tab === "Corpus" && <CorpusView onSelectItem={selItem} />}
          {tab === "Queries" && <QueriesView onSelectItem={selItem} onHighlight={(h) => { setHighlight(h); }} />}
          {tab === "Resolution" && <ResolutionView onSelectEntity={selEntity} />}
        </div>
        <Drawer selection={selection} onClose={() => setSelection(null)}
          onSelectItem={selItem} onSelectEntity={selEntity} />
      </div>
    </div>
  );
}

function ResolutionView({ onSelectEntity }) {
  const [merges, setMerges] = useState([]);
  useEffect(() => { api.merges().then(setMerges).catch(() => {}); }, []);
  return (
    <div className="pad">
      <div className="section-h">Entity resolution — {merges.length} merges (the hard sub-problem, made visible)</div>
      <p className="snippet">Each card is a set of distinct surface forms the system decided are the same real entity, with the reason. This is where duplicate identities across email + WhatsApp get unified.</p>
      {merges.map((m) => (
        <div key={m.canonical_id} className="merge">
          <div><strong>{m.rationale}</strong> <span className="chip" onClick={() => onSelectEntity(m.canonical_id)}>open entity →</span></div>
          <div className="forms">{m.merged_forms.map((f, i) => <span key={i} className="chip mono">{f}</span>)}</div>
        </div>
      ))}
    </div>
  );
}
