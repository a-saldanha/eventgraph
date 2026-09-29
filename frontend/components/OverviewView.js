"use client";

// The entry screen: frames the problem, shows live scale, and routes into the tools.
// Copy is intentionally plain JSX so it's easy to edit.

const PROBLEMS = [
  {
    icon: "🗂",
    title: "A multi-source mess",
    body: "Email, WhatsApp, a calendar export, and ticket/visa/receipt PDFs — different formats, overlapping content, out-of-order timestamps. Most of it isn't even about this trip.",
  },
  {
    icon: "🧩",
    title: "The same entity, many faces",
    body: "One person shows up as four email addresses and three spellings of their name. Resolving those to a single real entity — across channels — is the hard part.",
  },
  {
    icon: "🔊",
    title: "Signal vs. noise",
    body: "Illness, group chatter, unrelated work all sit in the same archive. The pipeline scopes relevance so a query about visa cost isn't polluted by a birthday thread.",
  },
  {
    icon: "🔗",
    title: "Dedup & provenance",
    body: "A registration email exists both as a standalone file and inside the Gmail mbox. Never double-count it — and trace every fact in an answer back to its exact source.",
  },
];

const FEATURES = [
  { tab: "Graph", icon: "◈", title: "Explore the graph", body: "People, orgs, places, money and sub-events, linked by who-paid-whom and who-talked-to-whom. Search, focus a node, walk its connections." },
  { tab: "Timeline", icon: "▤", title: "Follow the timeline", body: "The event unfolding across topic episodes — with date ranges, item counts, and links to the underlying messages." },
  { tab: "Queries", icon: "❓", title: "Ask anything", body: "Natural-language questions answered by navigating the graph, plus preset cross-source joins — every answer cites its sources." },
  { tab: "Corpus", icon: "▦", title: "Browse the corpus", body: "All source items, marked relevant or noise, filterable by channel and conversation. Click any fact through to the raw message." },
  { tab: "Resolution", icon: "⧉", title: "See the resolution", body: "The entity merges the system made and the reason for each — the cross-source identity work, made visible and auditable." },
  { tab: "Ingest", icon: "⬆", title: "Ingest new sources", body: "Drop in more files (email, WhatsApp, mbox, PDFs) and rebuild the graph, heuristically or with the LLM extractor." },
];

const ROADMAP = [
  "Postgres + Neo4j backing store (today it runs off a JSON snapshot)",
  "Auth-gated cloud deploy over the redacted corpus",
  "OCR & spreadsheet ingestion — images and Excel calendars",
  "Tighter near-duplicate recall on reworded forwards",
];

export default function OverviewView({ stats, onGo }) {
  const n = (v) => (v == null ? "—" : v.toLocaleString());
  const dupes = stats ? (stats.exact_dupes || 0) + (stats.near_dupes || 0) : null;

  const METRICS = [
    { v: stats?.items, label: "source items ingested" },
    { v: stats?.relevant_items, label: "scoped as relevant" },
    { v: dupes, label: "duplicates caught" },
    { v: stats?.entities, label: "resolved entities" },
    { v: stats?.merges, label: "identity merges" },
    { v: stats?.edges, label: "relationships" },
  ];

  return (
    <div className="landing">
      {/* Hero */}
      <section className="hero">
        <div className="hero-eyebrow">Cross-source event reconstruction</div>
        <h1 className="hero-title">
          From acceptance to Tampere — one paper's journey, made queryable.
        </h1>
        <p className="hero-sub">
          A knowledge graph of a single real event: an accepted paper's road to{" "}
          <strong>IEEE ICIP 2026</strong> in Tampere, Finland — reconstructed from a noisy,
          redundant personal archive of email, WhatsApp, a calendar and PDFs. Ask what it
          cost, what had to happen, and where things stand — and trace every answer back to
          the exact message it came from.
        </p>
        <div className="hero-cta">
          <button className="btn-primary" onClick={() => onGo("Graph")}>Explore the graph →</button>
          <button className="btn-ghost" onClick={() => onGo("Queries")}>Ask a question</button>
        </div>
      </section>

      {/* Live metrics */}
      <section className="metric-band">
        {METRICS.map((m) => (
          <div key={m.label} className="metric">
            <div className="metric-v">{n(m.v)}</div>
            <div className="metric-l">{m.label}</div>
          </div>
        ))}
      </section>

      {/* The hard problem */}
      <section className="land-section">
        <h2 className="land-h">The hard problem</h2>
        <p className="land-lead">
          This isn't "upload a document, get JSON." The archive is dirty on purpose — the
          interesting work is turning it into a clean, grounded event graph.
        </p>
        <div className="card-grid">
          {PROBLEMS.map((p) => (
            <div key={p.title} className="card">
              <div className="card-icon">{p.icon}</div>
              <div className="card-title">{p.title}</div>
              <div className="card-body">{p.body}</div>
            </div>
          ))}
        </div>
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

      {/* Roadmap */}
      <section className="land-section">
        <h2 className="land-h">Where this is going</h2>
        <ul className="roadmap">
          {ROADMAP.map((r) => (
            <li key={r}><span className="road-dot" />{r}</li>
          ))}
        </ul>
      </section>

      <footer className="land-foot">
        Redacted for privacy · a bounded single-event demo · every fact grounded to its source.
      </footer>
    </div>
  );
}
