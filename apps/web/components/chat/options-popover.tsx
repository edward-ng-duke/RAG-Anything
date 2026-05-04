"use client";
import { Settings } from "lucide-react";

export interface ChatOptions {
  mode: string;
  top_k: number;
  vlm_enhanced: boolean;
}

export function OptionsPopover({
  value,
  onChange,
}: {
  value: ChatOptions;
  onChange: (next: ChatOptions) => void;
}) {
  return (
    <details className="relative">
      <summary className="list-none inline-flex h-9 items-center justify-center px-2 rounded-md border cursor-pointer">
        <Settings size={14} />
      </summary>
      <div className="absolute right-0 mt-2 w-64 rounded-md border bg-popover p-3 shadow-md z-10 space-y-3 text-sm">
        <label className="block">
          <span className="text-muted-foreground">Mode</span>
          <select
            className="mt-1 block w-full h-8 rounded-md border bg-transparent px-2"
            value={value.mode}
            onChange={(e) => onChange({ ...value, mode: e.target.value })}
          >
            <option value="hybrid">hybrid</option>
            <option value="local">local</option>
            <option value="global">global</option>
            <option value="naive">naive</option>
            <option value="mix">mix</option>
          </select>
        </label>
        <label className="block">
          <span className="text-muted-foreground">top_k</span>
          <input
            type="number"
            min={1}
            max={50}
            value={value.top_k}
            onChange={(e) =>
              onChange({ ...value, top_k: Number(e.target.value) || 10 })
            }
            className="mt-1 block w-full h-8 rounded-md border bg-transparent px-2"
          />
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={value.vlm_enhanced}
            onChange={(e) =>
              onChange({ ...value, vlm_enhanced: e.target.checked })
            }
          />
          <span>vlm_enhanced</span>
        </label>
      </div>
    </details>
  );
}
