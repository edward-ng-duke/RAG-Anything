"use client";
import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import { getConversation } from "@/lib/api/conversations";
import { MessageList } from "@/components/chat/message-list";

export default function ChatPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading, error } = useQuery({
    queryKey: ["conversation", id],
    queryFn: () => getConversation(id),
  });

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (error) return <p className="text-sm text-destructive">Failed to load conversation.</p>;
  if (!data) return null;

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem-3rem)]">
      <div className="border-b pb-2 mb-2">
        <h1 className="text-lg font-semibold">{data.conversation.title || "Untitled"}</h1>
      </div>
      <MessageList messages={data.messages} />
      <div className="border-t pt-2 text-sm text-muted-foreground italic">
        Composer: implemented in next task
      </div>
    </div>
  );
}
