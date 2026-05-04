"use client";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import type { DocumentBrief } from "@/lib/api/types";
import Link from "next/link";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  indexed: "default",
  pending: "secondary",
  parsing: "secondary",
  failed: "destructive",
  deleted: "outline",
};

function formatBytes(n: number | null) {
  if (!n) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function DocumentTable({ items }: { items: DocumentBrief[] }) {
  if (items.length === 0) {
    return <div className="text-sm text-muted-foreground py-8 text-center">No documents yet.</div>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Filename</TableHead>
          <TableHead>Size</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Uploaded</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((d) => (
          <TableRow key={d.document_id}>
            <TableCell>
              <Link href={`/documents/${d.document_id}`} className="font-medium underline-offset-2 hover:underline">
                {d.file_name}
              </Link>
            </TableCell>
            <TableCell>{formatBytes(d.file_size)}</TableCell>
            <TableCell>
              <Badge variant={STATUS_VARIANT[d.status] ?? "outline"}>{d.status}</Badge>
            </TableCell>
            <TableCell className="text-muted-foreground">{new Date(d.uploaded_at).toLocaleString()}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
