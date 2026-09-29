// All requests go to the same-origin Next.js proxy at /api/...
// The proxy adds the backend auth token — no token is ever sent to the browser.
export const API_BASE = "";

async function j(path) {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

export const api = {
  stats: () => j("/api/stats"),
  graph: () => j("/api/graph"),
  merges: () => j("/api/merges"),
  timeline: () => j("/api/timeline"),
  items: (params = {}) => {
    const q = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null)
    );
    return j(`/api/items?${q}`);
  },
  item: (id) => j(`/api/item/${encodeURIComponent(id)}`),
  entity: (id) => j(`/api/entity/${encodeURIComponent(id)}`),
  query: (id) => j(`/api/query/${id}`),
  ask: async (q) => {
    const r = await fetch(`/api/ask?q=${encodeURIComponent(q)}`);
    if (r.status === 503) return r.json();
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  starterQuestions: () => j("/api/starter_questions"),
  currencyFlags: () => j("/api/flags/currency"),
  resolveCurrency: async (entityId, currency) => {
    const r = await fetch(
      `/api/resolve/currency?entity_id=${encodeURIComponent(entityId)}&currency=${currency}`,
      { method: "POST" }
    );
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  models: () => j("/api/capabilities/models"),
  capabilities: () => j("/api/capabilities"),
  ingest: async (fileList, mode = "heuristic") => {
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f);
    const r = await fetch(`/api/ingest?mode=${mode}`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  pollJob: async (jobId) => {
    const r = await fetch(`/api/jobs/${jobId}`);
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  replay: async (mode = "heuristic") => {
    const r = await fetch(`/api/replay?mode=${mode}`, { method: "POST" });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.message || `${r.status}`);
    }
    return r.json();
  },
  resetSession: async () => {
    const r = await fetch("/api/session/reset", { method: "POST" });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
};

// Poll a job until done or failed; calls onEvent for each new event, resolves with final state.
export async function pollJob(jobId, onEvent, intervalMs = 1000) {
  let seen = 0;
  while (true) {
    const data = await api.pollJob(jobId);
    const newEvents = data.events.slice(seen);
    seen = data.events.length;
    for (const evt of newEvents) onEvent(evt);
    if (data.status === "done" || data.status === "failed") {
      return data;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export const TYPE_COLORS = {
  person: "#2b6cb0",
  org: "#8a4baf",
  location: "#2f855a",
  money: "#b7791f",
  document: "#c05621",
  subevent: "#c53030",
};
