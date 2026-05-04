"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getDocument, deleteDocument } from "@/lib/api/documents";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { toast } from "sonner";
import type { DocumentBrief } from "@/lib/api/types";

export function DocumentDetail({ id }: { id: string }) {
  const router = useRouter();
  const qc = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["document", id],
    queryFn: () => getDocument(id) as Promise<DocumentBrief>,
  });

  const del = useMutation({
    mutationFn: () => deleteDocument(id),
    onSuccess: () => {
      toast.success("document deleted; rebuild queued");
      qc.invalidateQueries({ queryKey: ["documents"] });
      router.replace("/documents");
    },
    onError: (e: any) => {
      const msg = e?.response?.data?.detail || "delete failed";
      toast.error(typeof msg === "string" ? msg : "delete failed");
    },
  });

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (error) return <p className="text-sm text-destructive">Failed to load.</p>;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold break-all">{data.file_name}</h1>
          <div className="text-sm text-muted-foreground mt-1">
            <Badge variant="outline" className="mr-2">{data.status}</Badge>
            uploaded {new Date(data.uploaded_at).toLocaleString()}
            {data.indexed_at && <> · indexed {new Date(data.indexed_at).toLocaleString()}</>}
          </div>
        </div>
        <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
          <DialogTrigger asChild>
            <Button variant="destructive" size="sm">Delete</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Delete this document?</DialogTitle>
              <DialogDescription>
                Deleting will trigger a knowledge graph rebuild for this tenant.
                The operation can take several minutes for large corpora.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirmOpen(false)}>Cancel</Button>
              <Button variant="destructive" disabled={del.isPending} onClick={() => del.mutate()}>
                {del.isPending ? "Deleting…" : "Delete"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      <Card>
        <CardHeader><CardTitle>Metadata</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-2 gap-y-2 text-sm">
          <span className="text-muted-foreground">Document ID</span>
          <span className="font-mono break-all">{data.document_id}</span>
          <span className="text-muted-foreground">MIME type</span>
          <span>{data.mime_type ?? "—"}</span>
          <span className="text-muted-foreground">Size</span>
          <span>{data.file_size != null ? `${data.file_size} bytes` : "—"}</span>
          <span className="text-muted-foreground">Content hash</span>
          <span className="font-mono text-xs break-all">{data.content_hash}</span>
          {data.error_message && (
            <>
              <span className="text-muted-foreground">Error</span>
              <span className="text-destructive">{data.error_message}</span>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
