"use client";
import { useCallback, useEffect, useState } from "react";
import { api, onSlowResponse, pollJob } from "@/lib/api";
import ApiError from "@/components/ApiError";
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
  const [selection, setSelection] = useState(null);
  const [highlight, setHighlight] = useState([]);
  const [reloadKey, setReloadKey] = useState(0);

  const [serverSlow, setServerSlow] = useState(false);
  useEffect(() => onSlowResponse(setServerSlow), []);

  // "shared" = real archive, "replay" = viewer's replay, "upload" = viewer's upload
  const [dataset, setDataset] = useState("shared");
  const [hasReplay, setHasReplay] = useState(false);
  const [hasUpload, setHasUpload] = useState(false);

  // Replay job progress (managed at page level so both Overview and Ingest can share it)
  const [replayLog, setReplayLog] = useState([]);
  const [replayBusy, setReplayBusy] = useState(false);
  const [replayError, setReplayError] = useState(null);
  const [replayDone, setReplayDone] = useState(false);

  const [statsError, setStatsError] = useState(null);
  const loadStats = () => api.stats().then(setStats).catch((e) => setStatsError(String(e)));
  const refresh = () => { loadStats(); setReloadKey((k) => k + 1); };
  useEffect(() => { loadStats(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const selEntity = (id) => setSelection({ kind: "entity", id });
  const selItem = (id) => setSelection({ kind: "item", id });

  const triggerReplay = useCallback(async (mode = "heuristic") => {
    if (replayBusy) return;
    setReplayBusy(true);
    setReplayLog([]);
    setReplayError(null);
    setReplayDone(false);
    setTab("Ingest"); // show live progress
    try {
      const { job_id } = await api.replay(mode);
      const result = await pollJob(job_id, (evt) => setReplayLog((l) => [...l, evt]));
      if (result.status === "failed") {
        setReplayError(result.error || "Replay failed.");
      } else {
        setHasReplay(true);
        setDataset("replay");
        setReplayDone(true);
        refresh();
        setTab("Graph");
      }
    } catch (e) {
      setReplayError(String(e));
    } finally {
      setReplayBusy(false);
    }
  }, [replayBusy]);

  const switchDataset = useCallback(async (target) => {
    if (target === dataset) return;
    if (target === "shared") {
      try { await api.resetSession(); } catch {}
      setDataset("shared");
      refresh();
    } else {
      // replay/upload — session bundle already set; just switch label + refresh
      setDataset(target);
      refresh();
    }
  }, [dataset]);

  const handleIngested = useCallback(() => {
    setHasUpload(true);
    setDataset("upload");
    refresh();
    setTab("Graph");
  }, []);

  return (
    <div className="app">
      {serverSlow && (
        <div style={{
          background: "#fffbe6", borderBottom: "1px solid #f0c060",
          padding: "4px 14px", fontSize: 12, color: "#7a6000",
        }}>
          Waking up the server… (Railway cold-start takes up to 30 s on the first visit)
        </div>
      )}
      <div className="top">
        <span className="brand">EventGraph</span>
        <div className="tabs">
          {TABS.map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>
        <span className="spacer" />

        {/* Dataset switcher */}
        {(hasReplay || hasUpload) && (
          <div className="dataset-switcher">
            <button
              className={dataset === "shared" ? "active" : ""}
              onClick={() => switchDataset("shared")}
              title="The original prebuilt archive"
            >Real archive</button>
            {hasReplay && (
              <button
                className={dataset === "replay" ? "active" : ""}
                onClick={() => switchDataset("replay")}
              >Your replay</button>
            )}
            {hasUpload && (
              <button
                className={dataset === "upload" ? "active" : ""}
                onClick={() => switchDataset("upload")}
              >Your upload</button>
            )}
          </div>
        )}

        {stats && (
          <span className="stat">
            {stats.items} items → {stats.relevant_items} relevant · {stats.exact_dupes + stats.near_dupes} dupes ·
            {" "}{stats.entities} entities · {stats.edges} edges · {stats.merges} merges
          </span>
        )}
      </div>

      <div className="main">
        <div className="content" key={reloadKey}>
          {tab === "Overview" && (
            <OverviewView
              stats={stats}
              onGo={setTab}
              onReplay={triggerReplay}
              replayBusy={replayBusy}
            />
          )}
          {tab === "Ingest" && (
            <IngestView
              onIngested={handleIngested}
              onReplay={triggerReplay}
              replayLog={replayLog}
              replayBusy={replayBusy}
              replayError={replayError}
              replayDone={replayDone}
            />
          )}
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
  const [error, setError] = useState(null);
  const load = () => api.merges().then(setMerges).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="pad">
      <div className="section-h">Entity resolution — {merges.length} merges (the hard sub-problem, made visible)</div>
      <p className="snippet">Each card is a set of distinct surface forms the system decided are the same real entity, with the reason. This is where duplicate identities across email + WhatsApp get unified.</p>
      {error && <ApiError message={error} onRetry={() => { setError(null); load(); }} />}
      {merges.map((m) => (
        <div key={m.canonical_id} className="merge">
          <div><strong>{m.rationale}</strong> <span className="chip" onClick={() => onSelectEntity(m.canonical_id)}>open entity →</span></div>
          <div className="forms">{m.merged_forms.map((f, i) => <span key={i} className="chip mono">{f}</span>)}</div>
        </div>
      ))}
    </div>
  );
}
