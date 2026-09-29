"use client";
import { useEffect, useRef, useState } from "react";
import { api, pollJob } from "@/lib/api";

export default function IngestView({ onIngested }) {
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);
  const [doneStats, setDoneStats] = useState(null);
  const [error, setError] = useState(null);
  const [useLLM, setUseLLM] = useState(false);
  const [llmAvail, setLlmAvail] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => {
    api.capabilities().then((c) => setLlmAvail(c.llm_available)).catch(() => {});
  }, []);

  const addFiles = (list) => setFiles((prev) => [...prev, ...Array.from(list)]);

  const run = async () => {
    if (!files.length) return;
    setBusy(true);
    setLog([]);
    setDoneStats(null);
    setError(null);
    try {
      const { job_id } = await api.ingest(files, useLLM ? "llm" : "heuristic");
      const result = await pollJob(job_id, (evt) => setLog((l) => [...l, evt]));
      if (result.status === "failed") {
        setError(result.error || "Build failed.");
      } else {
        setDoneStats(result.stats);
        onIngested && onIngested();
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pad" style={{ maxWidth: 760 }}>
      <div className="section-h">Ingest files</div>
      <p className="snippet">
        Supported: WhatsApp <b>.txt</b> exports, <b>.eml</b>, <b>.mbox</b>, redacted <b>.md</b> batches.
        PDFs and spreadsheets are recognized; text is extracted where possible.
        Uploads stay in your browser session for 30 minutes and are never stored.
      </p>

      <div
        className={"upload" + (drag ? " drag" : "")}
        style={{
          border: "1.5px dashed var(--border-strong)", padding: 22,
          textAlign: "center", cursor: "pointer",
          background: drag ? "var(--bg-sunken)" : "#fff",
        }}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer.files); }}
      >
        <strong>Drop files here</strong> or click to browse
        <input
          ref={inputRef} type="file" multiple hidden
          accept=".md,.txt,.eml,.mbox,.pdf,.xlsx,.csv,.zip"
          onChange={(e) => addFiles(e.target.files)}
        />
      </div>

      {files.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="section-h">{files.length} file(s) queued</div>
          {files.map((f, i) => (
            <div key={i} className="mono snippet">
              {f.name} <span className="meta">({Math.round(f.size / 1024)} KB)</span>
            </div>
          ))}
          <label style={{
            display: "flex", alignItems: "center", gap: 6,
            marginTop: 10, opacity: llmAvail ? 1 : 0.5,
          }}>
            <input type="checkbox" checked={useLLM} disabled={!llmAvail || busy}
              onChange={(e) => setUseLLM(e.target.checked)} />
            <span>
              Use <b>model extraction</b> — slower, more accurate for arbitrary content.
              {llmAvail ? "" : " (no API key configured)"}
            </span>
          </label>
          <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
            <button className="active" disabled={busy} onClick={run}>
              {busy ? "Processing…" : `Build graph${useLLM ? " (with model)" : " (instant)"}`}
            </button>
            <button disabled={busy} onClick={() => setFiles([])}>Clear</button>
          </div>
        </div>
      )}

      {log.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="section-h">Progress</div>
          <div className="mono" style={{
            background: "#fff", border: "1px solid var(--border)",
            padding: 10, maxHeight: 320, overflow: "auto",
          }}>
            {log.map((e, i) => (
              <div key={i} style={{
                color: e.stage === "error" ? "var(--fail, #a12d2d)"
                  : e.stage === "done" ? "#2f855a" : "#333",
                padding: "1px 0",
              }}>
                <span className="meta">[{e.stage}]</span> {e.message}
              </div>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div className="note" style={{ marginTop: 12, color: "var(--fail, #a12d2d)" }}>
          {error}
        </div>
      )}

      {doneStats && (
        <div className="note" style={{ marginTop: 12 }}>
          Graph built from your upload: <b>{doneStats.items}</b> items,{" "}
          {doneStats.exact_dupes + doneStats.near_dupes} duplicates removed,{" "}
          <b>{doneStats.entities}</b> entities, {doneStats.edges} edges,{" "}
          <b>{doneStats.merges}</b> identity merges. Switch to Graph or Queries to explore.
        </div>
      )}
    </div>
  );
}
