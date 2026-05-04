"use client";
import { useState } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";

interface ApiKey {
  id: string;
  name: string;
  last4: string;
  created_at: string;
  revoked: boolean;
}

export default function ApiKeysPage() {
  const [name, setName] = useState("");
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [newPlaintext, setNewPlaintext] = useState<string | null>(null);

  const onCreate = () => {
    if (!name) return toast.error("name required");
    // Simulate locally so UI is exercisable
    const fake = `rag_${Math.random().toString(36).slice(2, 18)}`;
    const k: ApiKey = {
      id: crypto.randomUUID(),
      name,
      last4: fake.slice(-4),
      created_at: new Date().toISOString(),
      revoked: false,
    };
    setKeys((prev) => [k, ...prev]);
    setNewPlaintext(fake);
    setName("");
    toast.warning("local-only stub: backend /v1/api-keys not yet implemented");
  };

  const onRevoke = (id: string) => {
    setKeys((prev) => prev.map((k) => (k.id === id ? { ...k, revoked: true } : k)));
    toast.info("revoked locally; backend persistence pending");
  };

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">API keys</h2>
      <p className="text-sm text-muted-foreground">
        Programmatic access tokens. Backend persistence pending — keys here are
        local-only previews of the eventual UI.
      </p>

      <Card>
        <CardHeader><CardTitle className="text-base">Create new key</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="key-name">Name</Label>
            <Input id="key-name" placeholder="e.g. local-dev" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <Button onClick={onCreate}>Generate</Button>
          {newPlaintext && (
            <div className="rounded-md border bg-muted/30 p-3">
              <p className="text-sm font-medium mb-1">Copy this now — you won't see it again:</p>
              <code className="text-xs break-all">{newPlaintext}</code>
              <div className="mt-2 flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(newPlaintext);
                      toast.success("copied");
                    } catch {
                      toast.error("clipboard unavailable");
                    }
                  }}
                >
                  Copy
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setNewPlaintext(null)}>Dismiss</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">Active keys</CardTitle></CardHeader>
        <CardContent>
          {keys.length === 0 ? (
            <p className="text-sm text-muted-foreground">No keys yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Last 4</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((k) => (
                  <TableRow key={k.id}>
                    <TableCell>{k.name}</TableCell>
                    <TableCell className="font-mono">…{k.last4}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {new Date(k.created_at).toLocaleString()}
                    </TableCell>
                    <TableCell>
                      <Badge variant={k.revoked ? "outline" : "default"}>
                        {k.revoked ? "revoked" : "active"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => onRevoke(k.id)}
                        disabled={k.revoked}
                      >
                        Revoke
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
