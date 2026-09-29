/**
 * Site-wide password gate (edge runtime).
 *
 * Valid eg_pass cookie value: HMAC-SHA256(SITE_SECRET, "ok:" + SITE_PASSWORD_VERSION), hex.
 * Auth is disabled (local dev) when SITE_SECRET is not set.
 */
import { NextResponse } from "next/server";

const _enc = new TextEncoder();

async function expectedCookie() {
  const secret = process.env.SITE_SECRET || "";
  if (!secret) return null; // auth disabled — local dev without SITE_SECRET
  const version = process.env.SITE_PASSWORD_VERSION || "1";
  const key = await crypto.subtle.importKey(
    "raw",
    _enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, _enc.encode(`ok:${version}`));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Constant-time hex-string comparison (both must be 64 chars). */
function hexEqual(a, b) {
  if (a.length !== 64 || b.length !== 64) return false;
  const ab = _enc.encode(a);
  const bb = _enc.encode(b);
  let diff = 0;
  for (let i = 0; i < 64; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

export async function middleware(req) {
  const { pathname } = req.nextUrl;

  // Paths that bypass auth entirely
  if (
    pathname === "/login" ||
    pathname === "/api/login" ||
    pathname === "/api/logout" ||
    pathname.startsWith("/_next/") ||
    pathname === "/favicon.ico"
  ) {
    return NextResponse.next();
  }

  const expected = await expectedCookie();
  if (!expected) return NextResponse.next(); // auth disabled

  const cookie = req.cookies.get("eg_pass")?.value || "";
  if (hexEqual(cookie, expected)) return NextResponse.next();

  // Not authenticated
  if (pathname.startsWith("/api/")) {
    return NextResponse.json(
      { error: "locked", message: "Enter the site password." },
      { status: 401 },
    );
  }

  const url = new URL("/login", req.url);
  url.searchParams.set("next", pathname + req.nextUrl.search);
  return NextResponse.redirect(url, 307);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon\\.ico).*)"],
};
