"use client";
import { useEffect, useRef } from "react";
import { MessageBubble, type ChatMessage } from "./message-bubble";
import type { MessageResponse } from "@/lib/api/types";

export function MessageList({ messages }: { messages: (ChatMessage | MessageResponse)[] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: "smooth" });
  }, [messages.length]);

  return (
    <div ref={ref} className="flex-1 overflow-y-auto space-y-4 p-4">
      {messages.length === 0 && (
        <p className="text-sm text-muted-foreground text-center py-8">No messages yet.</p>
      )}
      {messages.map((m, i) => (
        <MessageBubble key={(m as any).message_id || i} message={m} />
      ))}
    </div>
  );
}
