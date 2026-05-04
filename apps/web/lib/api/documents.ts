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

export async function uploadDocument(file: File, onProgress?: (pct: number) => void) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await client.post<{
    job_id: string;
    document_id: string;
    status: string;
    deduplicated: boolean;
  }>("/v1/ingest", fd, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (e) => {
      if (onProgress && e.total) onProgress(Math.round((e.loaded * 100) / e.total));
    },
  });
  return r.data;
}
