import { createHmac, timingSafeEqual } from "crypto";
import { NextResponse } from "next/server";

const SITE_PASSWORD = process.env.SITE_PASSWORD || "";
const SITE_SECRET = process.env.SITE_SECRET || "";
const SITE_PASSWORD_VERSION = process.env.SITE_PASSWORD_VERSION || "1";

/** Cookie value = HMAC-SHA256(SITE_SECRET, "ok:" + SITE_PASSWORD_VERSION), hex. */
function cookieValue() {
  return createHmac("sha256", SITE_SECRET)
    .update(`ok:${SITE_PASSWORD_VERSION}`)
    .digest("hex");
}

/** Constant-time comparison (length-independent via HMAC). */
function safeEqual(a, b) {
  if (!SITE_SECRET) return a === b;
  const ha = createHmac("sha256", SITE_SECRET).update(a).digest();
  const hb = createHmac("sha256", SITE_SECRET).update(b).digest();
  try {
    return timingSafeEqual(ha, hb);
  } catch {
    return false;
  }
}

export async function POST(req) {
  let password = "";
  try {
    const body = await req.json();
    password = String(body?.password ?? "");
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  if (!SITE_PASSWORD || !safeEqual(password, SITE_PASSWORD)) {
    await new Promise((r) => setTimeout(r, 700));
    return NextResponse.json({ error: "Wrong password." }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  const isSecure = req.url.startsWith("https://");
  res.cookies.set("eg_pass", cookieValue(), {
    httpOnly: true,
    secure: isSecure,
    sameSite: "lax",
    path: "/",
    maxAge: 604800,
  });
  return res;
}
