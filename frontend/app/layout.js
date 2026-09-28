import "./globals.css";

export const metadata = {
  title: "Event Knowledge Graph",
  description: "A queryable knowledge graph over one real event's messy multi-source archive.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en"><body>{children}</body></html>
  );
}
