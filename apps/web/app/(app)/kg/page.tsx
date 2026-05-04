"use client";
import { useQuery } from "@tanstack/react-query";
import { kgStats } from "@/lib/api/kg";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function KgOverviewPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["kg", "stats"],
    queryFn: kgStats,
  });

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Knowledge Graph</h1>
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-destructive">Failed to load stats.</p>}
      {data && (
        <div className="grid gap-4 grid-cols-1 sm:grid-cols-3">
          <Card>
            <CardHeader><CardTitle className="text-sm font-medium">Entities</CardTitle></CardHeader>
            <CardContent><p className="text-3xl font-semibold">{data.entities}</p></CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle className="text-sm font-medium">Relations</CardTitle></CardHeader>
            <CardContent><p className="text-3xl font-semibold">{data.relations}</p></CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle className="text-sm font-medium">Chunks</CardTitle></CardHeader>
            <CardContent><p className="text-3xl font-semibold">{data.chunks}</p></CardContent>
          </Card>
        </div>
      )}
      <div className="flex gap-2">
        <Button asChild variant="outline"><Link href="/kg/entities">Browse entities →</Link></Button>
        <Button asChild variant="outline"><Link href="/kg/relations">Browse relations →</Link></Button>
        <Button asChild variant="outline"><Link href="/kg/explore">Explore subgraph →</Link></Button>
      </div>
    </div>
  );
}
