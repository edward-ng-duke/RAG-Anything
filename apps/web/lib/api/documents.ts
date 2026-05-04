import client from "./client";
import type { DocumentListResponse } from "./types";

export async function listDocuments(params: {
  cursor?: string | null;
  limit?: number;
  status?: string;
} = {}) {
  const r = await client.get<DocumentListResponse>("/v1/documents", { params });
  return r.data;
}

export async function getDocument(id: string) {
  const r = await client.get(`/v1/documents/${id}`);
  return r.data;
}

export async function deleteDocument(id: string) {
  const r = await client.delete(`/v1/documents/${id}`);
  return r.data;
}
