"use client";
import Link from "next/link";

export interface Citation {
  document_id?: string;
  file_name?: string;
  chunk_id?: string;
  score?: number;
  snippet?: string;
}

export function CitationCard({ source, index }: { source: Citation; index: number }) {
  const inner = (
    <div className="rounded-md border p-2 text-xs hover:bg-accent transition-colors">
      <div className="flex items-center gap-2">
        <span className="rounded bg-primary text-primary-foreground px-1 text-[10px] font-medium">
          {index + 1}
        </span>
        <span className="font-medium truncate">
          {source.file_name || source.document_id || "Source"}
        </span>
        {source.score != null && (
          <span className="ml-auto text-muted-foreground">
            {source.score.toFixed(2)}
          </span>
        )}
      </div>
      {source.snippet && (
        <p className="mt-1 text-muted-foreground line-clamp-2">{source.snippet}</p>
      )}
    </div>
  );

  if (source.document_id) {
    return (
      <Link href={`/documents/${source.document_id}`} className="block">
        {inner}
      </Link>
    );
  }
  return inner;
}
