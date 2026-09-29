"use client";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

function LoginForm() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  const params = useSearchParams();

  async function handleSubmit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (r.ok) {
        router.push(params.get("next") || "/");
      } else {
        setError("Wrong password.");
      }
    } catch {
      setError("Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      justifyContent: "center", minHeight: "100vh", padding: "0 24px",
    }}>
      <div style={{ width: "100%", maxWidth: 380 }}>
        <h1 style={{ margin: "0 0 8px", fontSize: 24, fontWeight: 700 }}>EventGraph</h1>
        <p style={{ margin: "0 0 24px", color: "#555" }}>
          Enter the password you were given.
        </p>
        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            autoFocus
            required
            style={{
              padding: "10px 14px", fontSize: 16,
              border: "1px solid #ccc", borderRadius: 4, width: "100%",
              boxSizing: "border-box",
            }}
          />
          <button
            type="submit"
            disabled={busy}
            style={{
              padding: "10px 14px", fontSize: 16, cursor: busy ? "default" : "pointer",
              background: "#1a56db", color: "#fff", border: "none", borderRadius: 4,
            }}
          >
            {busy ? "Checking…" : "Enter"}
          </button>
          {error && (
            <p style={{ margin: 0, color: "#a00", fontSize: 14 }}>{error}</p>
          )}
        </form>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
