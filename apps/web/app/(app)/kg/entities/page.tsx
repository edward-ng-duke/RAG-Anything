"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listKgEntities } from "@/lib/api/kg";
import { EntityTable } from "@/components/kg/entity-table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

export default function KgEntitiesPage() {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<(string | null)[]>([]);

  // simple debounce
  if (search !== debounced) {
    setTimeout(() => setDebounced(search), 0);  // Next render will trigger query
  }

  const { data, isLoading, error } = useQuery({
    queryKey: ["kg", "entities", debounced, cursor],
    queryFn: () => listKgEntities({ search: debounced || undefined, cursor, limit: 25 }),
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
      <h1 className="text-2xl font-semibold">Entities</h1>
      <div className="flex gap-2">
        <Input
          placeholder="Search by name…"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setCursor(null); setHistory([]); }}
        />
      </div>
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-destructive">Failed to load entities.</p>}
      {data && <EntityTable items={data.items} />}
      <div className="flex gap-2 justify-end">
        <Button variant="outline" disabled={history.length === 0} onClick={onPrev}>Previous</Button>
        <Button variant="outline" disabled={!data?.next_cursor} onClick={onNext}>Next</Button>
      </div>
    </div>
  );
}
