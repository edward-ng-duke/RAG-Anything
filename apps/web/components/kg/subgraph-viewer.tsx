"use client";
import { useEffect, useRef } from "react";

export interface GraphNode {
  id: string;
  label?: string | null;
  properties?: Record<string, unknown> | null;
}

export interface GraphEdge {
  source?: string | null;
  target?: string | null;
  type?: string | null;
  properties?: Record<string, unknown> | null;
}

export function SubgraphViewer({
  nodes,
  edges,
  height = 500,
  onNodeClick,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  height?: number;
  onNodeClick?: (id: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<any>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    let cleaned = false;

    (async () => {
      // dynamic imports — these libs ship UMD/ESM that pull DOM globals
      const Graph = (await import("graphology")).default;
      const Sigma = (await import("sigma")).default;
      const forceAtlas2 = (await import("graphology-layout-forceatlas2")).default;

      const graph = new Graph();
      for (const n of nodes) {
        if (graph.hasNode(n.id)) continue;
        graph.addNode(n.id, {
          label: n.label || n.id,
          x: Math.random(),
          y: Math.random(),
          size: 6,
          color: "#3b82f6",
        });
      }
      for (const e of edges) {
        if (!e.source || !e.target) continue;
        if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) continue;
        try {
          graph.addEdge(e.source, e.target, { label: e.type || "", color: "#94a3b8", size: 1 });
        } catch {
          // ignore parallel/duplicate edges
        }
      }
      // initial layout
      forceAtlas2.assign(graph, { iterations: 100, settings: { gravity: 1, scalingRatio: 10 } });

      if (cleaned) return;
      sigmaRef.current?.kill();
      sigmaRef.current = new Sigma(graph, containerRef.current!, {
        renderLabels: true,
      });
      if (onNodeClick) {
        sigmaRef.current.on("clickNode", (e: any) => onNodeClick(e.node));
      }
    })();

    return () => {
      cleaned = true;
      sigmaRef.current?.kill();
      sigmaRef.current = null;
    };
  }, [nodes, edges, onNodeClick]);

  return (
    <div
      ref={containerRef}
      style={{ height, width: "100%" }}
      className="rounded-md border bg-card"
    />
  );
}
