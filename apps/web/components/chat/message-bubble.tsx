"use client";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CitationCard, type Citation } from "./citation-card";
import type { MessageResponse } from "@/lib/api/types";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  sources?: Citation[];
  pending?: boolean;
}

export function MessageBubble({ message }: { message: ChatMessage | MessageResponse }) {
  const [showSources, setShowSources] = useState(false);
  const role = (message as ChatMessage).role || "assistant";
  const content = message.content;
  const rawSources = (message as any).sources;
  // sources may be {sources: [...]} or [...] depending on origin
  const sources: Citation[] = Array.isArray(rawSources)
    ? rawSources
    : Array.isArray(rawSources?.sources)
      ? rawSources.sources
      : [];

  const isUser = role === "user";
  return (
    <div className={isUser ? "flex justify-end" : "flex justify-start"}>
      <div
        className={[
          "max-w-2xl rounded-lg px-4 py-2 text-sm space-y-2",
          isUser ? "bg-primary text-primary-foreground" : "bg-muted",
        ].join(" ")}
      >
        <div className={isUser ? "" : "prose prose-sm max-w-none"}>
          {isUser ? (
            <p className="whitespace-pre-wrap">{content}</p>
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          )}
        </div>
        {!isUser && sources.length > 0 && (
          <div>
            <button
              type="button"
              onClick={() => setShowSources((v) => !v)}
              className="text-xs underline opacity-70 hover:opacity-100"
            >
              {showSources ? "Hide" : "Show"} {sources.length} source{sources.length === 1 ? "" : "s"}
            </button>
            {showSources && (
              <div className="grid gap-2 mt-2">
                {sources.map((s, i) => (
                  <CitationCard key={i} source={s} index={i} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
