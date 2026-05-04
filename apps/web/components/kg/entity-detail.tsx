"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { getKgEntity, getKgNeighbors } from "@/lib/api/kg";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SubgraphViewer } from "./subgraph-viewer";

export function EntityDetail({ id }: { id: string }) {
  const router = useRouter();
  const [depth, setDepth] = useState<1 | 2 | 3>(1);

  const { data: entity, isLoading: loadingE, error: errE } = useQuery({
    queryKey: ["kg", "entity", id],
    queryFn: () => getKgEntity(id),
  });

  const { data: neigh, isLoading: loadingN, error: errN } = useQuery({
    queryKey: ["kg", "entity", id, "neighbors", depth],
    queryFn: () => getKgNeighbors(id, depth),
    enabled: !!id,
  });

  if (loadingE) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (errE) return <p className="text-sm text-destructive">Failed to load entity.</p>;
  if (!entity) return null;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold break-all">{entity.entity_name || entity.id}</h1>
        <div className="text-sm text-muted-foreground mt-1">
          {entity.entity_type && <Badge variant="outline" className="mr-2">{entity.entity_type}</Badge>}
          {entity.file_path && <span>source: {entity.file_path}</span>}
        </div>
      </div>

      {entity.content && (
        <Card>
          <CardHeader><CardTitle className="text-base">Description</CardTitle></CardHeader>
          <CardContent className="text-sm whitespace-pre-wrap">{entity.content}</CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">Neighbors</CardTitle>
          <div className="flex gap-1">
            {[1, 2, 3].map((d) => (
              <Button
                key={d}
                size="sm"
                variant={depth === d ? "default" : "outline"}
                onClick={() => setDepth(d as 1 | 2 | 3)}
              >
                depth {d}
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent>
          {loadingN && <p className="text-sm text-muted-foreground">Loading subgraph…</p>}
          {errN && <p className="text-sm text-destructive">Failed to load subgraph.</p>}
          {neigh && (
            <SubgraphViewer
              nodes={neigh.nodes}
              edges={neigh.edges}
              height={420}
              onNodeClick={(nid) => {
                if (nid !== id) router.push(`/kg/entities/${encodeURIComponent(nid)}`);
              }}
            />
          )}
          {neigh && neigh.nodes.length === 0 && (
            <p className="text-sm text-muted-foreground">No neighbors at depth {depth}.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
