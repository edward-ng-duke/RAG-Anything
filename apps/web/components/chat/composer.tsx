"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Send } from "lucide-react";
import { OptionsPopover, type ChatOptions } from "./options-popover";

export interface ComposerProps {
  disabled?: boolean;
  options: ChatOptions;
  onOptionsChange: (next: ChatOptions) => void;
  onSend: (content: string) => void;
}

export function Composer({
  disabled,
  options,
  onOptionsChange,
  onSend,
}: ComposerProps) {
  const [text, setText] = useState("");

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  };

  return (
    <div className="border-t p-2">
      <div className="flex items-end gap-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          placeholder="Ask a question…"
          className="flex-1 resize-none rounded-md border bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <OptionsPopover value={options} onChange={onOptionsChange} />
        <Button size="icon" onClick={submit} disabled={disabled || !text.trim()}>
          <Send size={16} />
        </Button>
      </div>
    </div>
  );
}
