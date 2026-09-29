// Server-side proxy: forwards every /api/* request to the backend.
// BACKEND_URL and BACKEND_TOKEN are server-only env vars — never exposed to the browser.
// Using same-origin relative paths in the client eliminates any need for public auth tokens.

export const dynamic = "force-dynamic";
export const maxDuration = 60; // seconds — enough for uploads

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const BACKEND_TOKEN = process.env.BACKEND_TOKEN || "";

function buildHeaders(req) {
  const h = {};
  if (BACKEND_TOKEN) {
    h["Authorization"] = `Bearer ${BACKEND_TOKEN}`;
  }
  const cookie = req.headers.get("cookie");
  if (cookie) h["cookie"] = cookie;
  // Trust the IP the proxy received; the backend reads x-forwarded-for for rate limiting.
  const xff = req.headers.get("x-forwarded-for") || req.headers.get("x-real-ip");
  if (xff) h["x-forwarded-for"] = xff;
  const ct = req.headers.get("content-type");
  if (ct) h["content-type"] = ct;
  return h;
}

async function proxy(req, context) {
  const { path } = await context.params;

  // Admin endpoints are backend-internal only; the proxy never forwards them.
  if (path[0] === "admin" || path.join("/").startsWith("admin/")) {
    return new Response(JSON.stringify({ error: "not_found" }), {
      status: 404,
      headers: { "content-type": "application/json" },
    });
  }

  const url = new URL(req.url);
  const target = `${BACKEND_URL}/api/${path.join("/")}${url.search}`;

  const headers = buildHeaders(req);
  const method = req.method;
  let body = undefined;
  if (method !== "GET" && method !== "HEAD") {
    body = await req.arrayBuffer();
  }

  let backendRes;
  try {
    backendRes = await fetch(target, { method, headers, body });
  } catch (err) {
    return new Response(JSON.stringify({ error: "backend_unreachable", message: String(err) }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }

  const respHeaders = new Headers();
  for (const [k, v] of backendRes.headers.entries()) {
    const lk = k.toLowerCase();
    if (lk === "set-cookie") {
      // Headers.append accumulates multiple Set-Cookie values correctly.
      respHeaders.append("set-cookie", v);
    } else if (lk !== "transfer-encoding" && lk !== "connection") {
      respHeaders.set(k, v);
    }
  }

  return new Response(backendRes.body, { status: backendRes.status, headers: respHeaders });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const HEAD = proxy;
export const OPTIONS = proxy;
