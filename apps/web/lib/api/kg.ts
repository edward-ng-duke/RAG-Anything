import client from "./client";
import type { KGStats, KGEntity } from "./types";

export async function kgStats() {
  const r = await client.get<KGStats>("/v1/kg/stats");
  return r.data;
}

export async function listKgEntities(params: {
  type?: string;
  search?: string;
  cursor?: string | null;
  limit?: number;
} = {}) {
  const r = await client.get<{ items: KGEntity[]; next_cursor: string | null }>(
    "/v1/kg/entities",
    { params },
  );
  return r.data;
}

export async function getKgEntity(id: string) {
  const r = await client.get<KGEntity>(`/v1/kg/entities/${encodeURIComponent(id)}`);
  return r.data;
}

export async function getKgNeighbors(id: string, depth = 1) {
  const r = await client.get<{ nodes: any[]; edges: any[] }>(
    `/v1/kg/entities/${encodeURIComponent(id)}/neighbors`,
    { params: { depth } },
  );
  return r.data;
}

export async function getKgSubgraph(entities: string[], depth = 2) {
  const r = await client.get<{ nodes: any[]; edges: any[] }>("/v1/kg/subgraph", {
    params: { entities: entities.join(","), depth },
  });
  return r.data;
}

export async function listKgRelations(params: {
  source?: string;
  target?: string;
  type?: string;
  cursor?: string | null;
  limit?: number;
} = {}) {
  const r = await client.get<{ items: any[]; next_cursor: string | null }>(
    "/v1/kg/relations",
    { params },
  );
  return r.data;
}
