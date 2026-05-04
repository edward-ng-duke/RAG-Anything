"use client";
import { useCallback, useState } from "react";
import { getKgNeighbors, getKgSubgraph } from "@/lib/api/kg";
import { SubgraphViewer, type GraphNode, type GraphEdge } from "./subgraph-viewer";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

export function ExploreCanvas() {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [seed, setSeed] = useState("");
  const [selected, setSelected] = useState<GraphNode | null>(null);

  const mergeGraph = useCallback((newNodes: GraphNode[], newEdges: GraphEdge[]) => {
    setNodes((prev) => {
      const seen = new Set(prev.map((n) => n.id));
      const out = [...prev];
      for (const n of newNodes) if (!seen.has(n.id)) { seen.add(n.id); out.push(n); }
      return out;
    });
    setEdges((prev) => {
      const seen = new Set(prev.map((e) => `${e.source}|${e.target}|${e.type ?? ""}`));
      const out = [...prev];
      for (const e of newEdges) {
        const k = `${e.source}|${e.target}|${e.type ?? ""}`;
        if (!seen.has(k)) { seen.add(k); out.push(e); }
      }
      return out;
    });
  }, []);

  const seedFrom = async () => {
    if (!seed.trim()) return;
    try {
      const sub = await getKgSubgraph([seed.trim()], 1);
      setNodes(sub.nodes); setEdges(sub.edges);
      const found = sub.nodes.find((n) => n.id === seed.trim()) || sub.nodes[0] || null;
      setSelected(found);
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || "failed to seed");
    }
  };

  const expandNode = useCallback(async (id: string) => {
    try {
      const more = await getKgNeighbors(id, 1);
      mergeGraph(more.nodes, more.edges);
      const newSelected = nodes.find((n) => n.id === id) || more.nodes.find((n) => n.id === id) || null;
      setSelected(newSelected);
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || "failed to expand");
    }
  }, [mergeGraph, nodes]);

  return (
    <div className="grid grid-cols-3 gap-4 h-[calc(100vh-3.5rem-3rem-4rem)]">
      <div className="col-span-2 space-y-2 flex flex-col">
        <div className="flex gap-2">
          <Input placeholder="seed entity id…" value={seed} onChange={(e) => setSeed(e.target.value)} />
          <Button onClick={seedFrom}>Seed</Button>
          <Button variant="outline" onClick={() => { setNodes([]); setEdges([]); setSelected(null); }}>
            Clear
          </Button>
        </div>
        <div className="flex-1 min-h-0">
          <SubgraphViewer
            nodes={nodes}
            edges={edges}
            height={500}
            onNodeClick={expandNode}
          />
        </div>
        <p className="text-xs text-muted-foreground">
          Click a node to expand its neighbors. Nodes/edges are merged into the canvas.
        </p>
      </div>

      <aside className="border rounded-md p-3 overflow-auto">
        <h3 className="font-medium mb-2">Inspector</h3>
        {!selected && <p className="text-sm text-muted-foreground">Click a node to inspect.</p>}
        {selected && (
          <div className="space-y-2 text-sm">
            <div>
              <div className="text-xs text-muted-foreground">id</div>
              <div className="font-mono break-all">{selected.id}</div>
            </div>
            {selected.label && (
              <div>
                <div className="text-xs text-muted-foreground">label</div>
                <div>{selected.label}</div>
              </div>
            )}
            {selected.properties && (
              <div>
                <div className="text-xs text-muted-foreground">properties</div>
                <pre className="text-xs whitespace-pre-wrap break-all">
                  {JSON.stringify(selected.properties, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}
