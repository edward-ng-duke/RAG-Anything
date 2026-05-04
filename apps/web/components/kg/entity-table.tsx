"use client";
import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { KGEntity } from "@/lib/api/types";

export function EntityTable({ items }: { items: KGEntity[] }) {
  if (items.length === 0) {
    return <div className="text-sm text-muted-foreground py-8 text-center">No entities.</div>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Type</TableHead>
          <TableHead>Source</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((e) => (
          <TableRow key={e.id}>
            <TableCell>
              <Link
                href={`/kg/entities/${encodeURIComponent(e.id)}`}
                className="font-medium underline-offset-2 hover:underline"
              >
                {e.entity_name || e.id}
              </Link>
            </TableCell>
            <TableCell className="text-muted-foreground">{e.entity_type ?? "—"}</TableCell>
            <TableCell className="text-muted-foreground truncate max-w-md">
              {e.file_path ?? "—"}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
