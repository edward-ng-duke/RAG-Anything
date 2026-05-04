"use client";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "@/lib/api/documents";
import { DocumentTable } from "@/components/documents/document-table";
import { UploadDropzone } from "@/components/documents/upload-dropzone";

export default function DocumentsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
    refetchInterval: 5000,  // poll for status updates
  });

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Documents</h1>
      <UploadDropzone />
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-destructive">Failed to load documents.</p>}
      {data && <DocumentTable items={data.items} />}
    </div>
  );
}
