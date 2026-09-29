export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

// APP_TOKEN: set NEXT_PUBLIC_APP_TOKEN in Vercel env vars to match the backend APP_TOKEN.
// Leave unset for local dev (no auth gate by default).
const _token = process.env.NEXT_PUBLIC_APP_TOKEN || "";

function _headers(extra = {}) {
  return _token
    ? { Authorization: `Bearer ${_token}`, ...extra }
    : extra;
}

async function j(path) {
  const r = await fetch(`${API_BASE}${path}`, { headers: _headers() });
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
    const r = await fetch(`${API_BASE}/api/ask?q=${encodeURIComponent(q)}`, { headers: _headers() });
    if (r.status === 503) return r.json();  // return 503 body (error object) instead of throwing
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  starterQuestions: () => j("/api/starter_questions"),
  currencyFlags: () => j("/api/flags/currency"),
  resolveCurrency: async (entityId, currency) => {
    const r = await fetch(
      `${API_BASE}/api/resolve/currency?entity_id=${encodeURIComponent(entityId)}&currency=${currency}`,
      { method: "POST", headers: _headers() }
    );
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  models: () => j("/api/capabilities/models"),
  capabilities: () => j("/api/capabilities"),
  ingest: async (fileList, mode = "heuristic") => {
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f);
    const r = await fetch(`${API_BASE}/api/ingest?mode=${mode}`, {
      method: "POST", body: fd, headers: _headers(),
    });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
};

// Stream ingestion progress over SSE; resolves with {error, stats} on done.
export function streamJob(jobId, onEvent) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`${API_BASE}/api/jobs/${jobId}/events`);
    es.addEventListener("progress", (e) => onEvent(JSON.parse(e.data)));
    es.addEventListener("done", (e) => { es.close(); resolve(JSON.parse(e.data)); });
    es.addEventListener("error", () => { es.close(); reject(new Error("SSE error")); });
  });
}

export const TYPE_COLORS = {
  person: "#2b6cb0",
  org: "#8a4baf",
  location: "#2f855a",
  money: "#b7791f",
  document: "#c05621",
  subevent: "#c53030",
};
