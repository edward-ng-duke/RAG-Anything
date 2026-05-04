"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listKgRelations } from "@/lib/api/kg";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

export default function KgRelationsPage() {
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<(string | null)[]>([]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["kg", "relations", source, target, cursor],
    queryFn: () =>
      listKgRelations({
        source: source || undefined,
        target: target || undefined,
        cursor,
        limit: 25,
      }),
  });

  const onNext = () => {
    if (data?.next_cursor) {
      setHistory((h) => [...h, cursor]);
      setCursor(data.next_cursor);
    }
  };
  const onPrev = () => {
    setHistory((h) => {
      const prev = h.slice(0, -1);
      setCursor(h[h.length - 1] ?? null);
      return prev;
    });
  };

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Relations</h1>
      <div className="flex gap-2">
        <Input
          placeholder="source id…"
          value={source}
          onChange={(e) => { setSource(e.target.value); setCursor(null); setHistory([]); }}
        />
        <Input
          placeholder="target id…"
          value={target}
          onChange={(e) => { setTarget(e.target.value); setCursor(null); setHistory([]); }}
        />
      </div>
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-destructive">Failed to load relations.</p>}
      {data && (data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No relations.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Source</TableHead>
              <TableHead>Target</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Source file</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.items.map((r: any) => (
              <TableRow key={r.id}>
                <TableCell>{r.source_id ?? "—"}</TableCell>
                <TableCell>{r.target_id ?? "—"}</TableCell>
                <TableCell className="text-muted-foreground">{r.type ?? "—"}</TableCell>
                <TableCell className="text-muted-foreground truncate max-w-md">
                  {r.file_path ?? "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ))}
      <div className="flex gap-2 justify-end">
        <Button variant="outline" disabled={history.length === 0} onClick={onPrev}>Previous</Button>
        <Button variant="outline" disabled={!data?.next_cursor} onClick={onNext}>Next</Button>
      </div>
    </div>
  );
}
