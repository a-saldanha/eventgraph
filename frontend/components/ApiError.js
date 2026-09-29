"use client";

/**
 * Shows a backend error with a Retry button.
 *
 * Usage:
 *   const [error, setError] = useState(null);
 *   ...
 *   if (error) return <ApiError message={error} onRetry={() => { setError(null); load(); }} />;
 */
export default function ApiError({ message, onRetry }) {
  return (
    <div style={{
      padding: "24px 20px", color: "#7a1e1e",
      background: "#fff8f8", border: "1px solid #f0c0c0",
      borderRadius: 4, display: "flex", alignItems: "center",
      gap: 12, marginTop: 16,
    }}>
      <span style={{ flex: 1, fontSize: 13 }}>
        {message || "Something went wrong talking to the server."}
      </span>
      {onRetry && (
        <button onClick={onRetry} style={{ flexShrink: 0 }}>Retry</button>
      )}
    </div>
  );
}
