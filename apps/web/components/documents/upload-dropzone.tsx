"use client";
import { useCallback, useState, useRef } from "react";
import { uploadDocument } from "@/lib/api/documents";
import { Upload } from "lucide-react";
import { toast } from "sonner";
import { useQueryClient } from "@tanstack/react-query";

interface UploadEntry {
  file: File;
  progress: number;
  status: "uploading" | "queued" | "error";
  jobId?: string;
  message?: string;
}

export function UploadDropzone() {
  const [entries, setEntries] = useState<UploadEntry[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const qc = useQueryClient();

  const beginUpload = useCallback(async (file: File) => {
    setEntries((prev) => [...prev, { file, progress: 0, status: "uploading" }]);
    try {
      const result = await uploadDocument(file, (pct) => {
        setEntries((prev) => prev.map((e) =>
          e.file === file ? { ...e, progress: pct } : e
        ));
      });
      setEntries((prev) => prev.map((e) =>
        e.file === file ? { ...e, status: "queued", jobId: result.job_id } : e
      ));
      qc.invalidateQueries({ queryKey: ["documents"] });
      if (result.deduplicated) {
        toast.info(`${file.name}: already uploaded`);
      } else {
        toast.success(`${file.name}: queued`);
      }
    } catch (e: any) {
      const msg = e?.response?.data?.detail || "upload failed";
      setEntries((prev) => prev.map((entry) =>
        entry.file === file ? { ...entry, status: "error", message: msg } : entry
      ));
      toast.error(`${file.name}: ${msg}`);
    }
  }, [qc]);

  const onFiles = (files: FileList | null) => {
    if (!files) return;
    Array.from(files).forEach((f) => beginUpload(f));
  };

  return (
    <div className="space-y-2">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          onFiles(e.dataTransfer.files);
        }}
        className={[
          "rounded-lg border-2 border-dashed p-8 text-center transition-colors cursor-pointer",
          dragOver ? "border-primary bg-accent/30" : "border-muted-foreground/30",
        ].join(" ")}
        onClick={() => inputRef.current?.click()}
      >
        <Upload className="mx-auto mb-2" size={24} />
        <p className="text-sm text-muted-foreground">Drag files here or click to upload</p>
        <input
          ref={inputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => onFiles(e.target.files)}
        />
      </div>
      {entries.length > 0 && (
        <ul className="space-y-1">
          {entries.map((e, i) => (
            <li key={`${e.file.name}-${i}`} className="text-sm flex items-center gap-3">
              <span className="flex-1 truncate">{e.file.name}</span>
              {e.status === "uploading" && (
                <span className="text-muted-foreground">{e.progress}%</span>
              )}
              {e.status === "queued" && <span className="text-primary">queued</span>}
              {e.status === "error" && <span className="text-destructive">{e.message}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
