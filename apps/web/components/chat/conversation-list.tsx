"use client";
import Link from "next/link";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listConversations, createConversation } from "@/lib/api/conversations";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

export function ConversationList() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["conversations"],
    queryFn: listConversations,
  });

  const createMut = useMutation({
    mutationFn: () => createConversation(),
    onSuccess: (convo) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      router.push(`/chat/${convo.conversation_id}`);
    },
    onError: () => toast.error("failed to create conversation"),
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Conversations</h2>
        <Button size="sm" onClick={() => createMut.mutate()} disabled={createMut.isPending}>
          <Plus size={14} className="mr-1" /> New
        </Button>
      </div>
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {data && data.length === 0 && (
        <p className="text-sm text-muted-foreground">No conversations yet.</p>
      )}
      <ul className="space-y-1">
        {data?.map((c) => (
          <li key={c.conversation_id}>
            <Link
              href={`/chat/${c.conversation_id}`}
              className="block rounded-md px-3 py-2 text-sm hover:bg-accent truncate"
            >
              {c.title || "Untitled"}
              <div className="text-xs text-muted-foreground">
                {new Date(c.updated_at).toLocaleString()}
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
