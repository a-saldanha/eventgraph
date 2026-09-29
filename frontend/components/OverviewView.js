"use client";

const FEATURES = [
  { tab: "Graph",      icon: "◈", title: "Explore the graph",    body: "People, orgs, places, money and events linked by who-paid-whom and who-talked-to-whom. Search and walk connections." },
  { tab: "Timeline",   icon: "▤", title: "Follow the timeline",   body: "The event unfolding across topic episodes — with date ranges, item counts, and links to the underlying messages." },
  { tab: "Queries",    icon: "❓", title: "Ask anything",          body: "Natural-language questions answered by navigating the graph. Every answer cites the exact source message." },
  { tab: "Resolution", icon: "⧉", title: "See the resolution",    body: "The identity merges the system made — the same person appearing as four email addresses, unified and auditable." },
];

export default function OverviewView({ stats, onGo, onReplay, replayBusy }) {
  const n = (v) => (v == null ? "—" : v.toLocaleString());
  const dupes = stats ? (stats.exact_dupes || 0) + (stats.near_dupes || 0) : null;

  const BUILD_METRICS = [
    { v: stats?.items,            label: "source items" },
    { v: stats?.relevant_items,   label: "scoped as relevant" },
    { v: dupes,                   label: "duplicates caught" },
    { v: stats?.entities,         label: "resolved entities" },
    { v: stats?.merges,           label: "identity merges" },
    { v: stats?.edges,            label: "relationships" },
  ];

  return (
    <div className="landing">

      {/* One-sentence description */}
      <section className="hero" style={{ paddingBottom: 0 }}>
        <h1 className="hero-title" style={{ fontSize: 22, marginBottom: 6 }}>
          EventGraph
        </h1>
        <p className="hero-sub" style={{ maxWidth: 640 }}>
          A queryable knowledge graph of one real event — reconstructed from a noisy,
          multi-format personal archive of email, WhatsApp, calendar exports, and PDFs.
          Every fact is grounded back to the exact source message.
        </p>
        <div className="hero-cta">
          <button className="btn-primary" onClick={() => onGo("Graph")}>Explore the graph →</button>
          <button className="btn-ghost" onClick={() => onGo("Queries")}>Ask a question</button>
          <button
            className="btn-ghost"
            disabled={replayBusy}
            onClick={() => onReplay && onReplay("heuristic")}
            title="Runs the real pipeline on the 4 original source files. Cached — costs nothing."
          >
            {replayBusy ? "Replaying…" : "Replay the upload →"}
          </button>
        </div>
      </section>

      {/* Build report */}
      <section className="metric-band">
        {BUILD_METRICS.map((m) => (
          <div key={m.label} className="metric">
            <div className="metric-v">{n(m.v)}</div>
            <div className="metric-l">{m.label}</div>
          </div>
        ))}
      </section>

      {/* What you can do */}
      <section className="land-section">
        <h2 className="land-h">What you can do</h2>
        <div className="card-grid">
          {FEATURES.map((f) => (
            <button key={f.tab} className="card card-link" onClick={() => onGo(f.tab)}>
              <div className="card-icon">{f.icon}</div>
              <div className="card-title">{f.title} <span className="card-arrow">→</span></div>
              <div className="card-body">{f.body}</div>
            </button>
          ))}
        </div>
      </section>

      <footer className="land-foot">
        A bounded single-event demo · every fact grounded to its source.
      </footer>
    </div>
  );
}
