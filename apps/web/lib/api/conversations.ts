import client from "./client";
import type { ConversationBrief, ConversationDetailResponse } from "./types";

export async function listConversations() {
  const r = await client.get<{ items: ConversationBrief[] }>("/v1/conversations");
  return r.data.items;
}

export async function createConversation(title?: string) {
  const r = await client.post<ConversationBrief>("/v1/conversations", { title });
  return r.data;
}

export async function getConversation(id: string) {
  const r = await client.get<ConversationDetailResponse>(`/v1/conversations/${id}`);
  return r.data;
}

export async function deleteConversation(id: string) {
  await client.delete(`/v1/conversations/${id}`);
}
