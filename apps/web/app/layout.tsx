import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RAG-Anything",
  description: "Multimodal RAG with knowledge graph",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-background antialiased">{children}</body>
    </html>
  );
}
