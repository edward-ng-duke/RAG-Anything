"use client";
import { use, useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getConversation } from "@/lib/api/conversations";
import { MessageList } from "@/components/chat/message-list";
import { Composer } from "@/components/chat/composer";
import { type ChatMessage } from "@/components/chat/message-bubble";
import { streamSSE } from "@/lib/api/sse";
import { useAuth } from "@/lib/stores/auth";

export default function ChatPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const accessToken = useAuth((s) => s.accessToken);
  const [pending, setPending] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["conversation", id],
    queryFn: () => getConversation(id),
  });

  const [options, setOptions] = useState({
    mode: "hybrid",
    top_k: 10,
    vlm_enhanced: false,
  });

  const onSend = useCallback(
    async (content: string) => {
      setStreaming(true);
      setPending([
        { role: "user", content },
        { role: "assistant", content: "", pending: true },
      ]);

      const apiBase =
        process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
      let assistantText = "";
      try {
        const stream = streamSSE(
          `${apiBase}/v1/conversations/${id}/messages`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: accessToken ? `Bearer ${accessToken}` : "",
            },
            body: JSON.stringify({ content, ...options }),
          },
        );
        for await (const ev of stream) {
          if (ev.event === "delta" && ev.data?.content) {
            assistantText += ev.data.content;
            setPending([
              { role: "user", content },
              { role: "assistant", content: assistantText, pending: true },
            ]);
          } else if (ev.event === "done") {
            break;
          } else if (ev.event === "error") {
            assistantText = `Error: ${ev.data?.message || "unknown"}`;
            setPending([
              { role: "user", content },
              { role: "assistant", content: assistantText },
            ]);
            break;
          }
        }
      } catch (e: any) {
        setPending([
          { role: "user", content },
          {
            role: "assistant",
            content: `Error: ${e?.message || "request failed"}`,
          },
        ]);
      } finally {
        setStreaming(false);
        // Refetch authoritative state from backend
        await refetch();
        setPending([]);
      }
    },
    [id, accessToken, options, refetch],
  );

  if (isLoading)
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (error)
    return (
      <p className="text-sm text-destructive">Failed to load conversation.</p>
    );
  if (!data) return null;

  const messages =
    pending.length > 0 ? [...data.messages, ...pending] : data.messages;

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem-3rem)]">
      <div className="border-b pb-2 mb-2">
        <h1 className="text-lg font-semibold">
          {data.conversation.title || "Untitled"}
        </h1>
      </div>
      <MessageList messages={messages} />
      <Composer
        disabled={streaming}
        options={options}
        onOptionsChange={setOptions}
        onSend={onSend}
      />
    </div>
  );
}
