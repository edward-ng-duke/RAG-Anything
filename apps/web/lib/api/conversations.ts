import client from "./client";
import type { ConversationBrief, ConversationDetailResponse } from "./types";

/**
 * Path for sending a message to a conversation.
 *
 * Note: messages are streamed via Server-Sent Events. There is no `sendMessage`
 * helper in this module — SSE goes through `lib/api/sse.ts` directly using
 * `streamSSE(url, init)` so the caller can iterate `delta`/`done`/`error`
 * events as they arrive. See `app/(app)/chat/[id]/page.tsx` for usage.
 */
export const MESSAGES_PATH = (id: string) => `/v1/conversations/${id}/messages`;

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
