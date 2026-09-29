import "./globals.css";

export const metadata = {
  title: "EventGraph",
  description: "A queryable knowledge graph of one real event — reconstructed from email, WhatsApp, calendar and PDFs.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en"><body>{children}</body></html>
  );
}
