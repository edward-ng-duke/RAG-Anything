"use client";
import { useQuery } from "@tanstack/react-query";
import { getCurrentTenant } from "@/lib/api/tenants";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

export default function TenantSettingsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["tenants", "me"],
    queryFn: getCurrentTenant,
  });

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">Tenant</h2>
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-destructive">Failed to load.</p>}
      {data && (
        <>
          <Card>
            <CardHeader><CardTitle className="text-base">Identity</CardTitle></CardHeader>
            <CardContent className="grid grid-cols-2 gap-y-2 text-sm">
              <span className="text-muted-foreground">Tenant ID</span>
              <span className="font-mono break-all">{data.tenant_id}</span>
              <span className="text-muted-foreground">Display name</span>
              <span>{data.display_name}</span>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle className="text-base">Storage</CardTitle></CardHeader>
            <CardContent className="grid grid-cols-2 gap-y-2 text-sm">
              <span className="text-muted-foreground">Used</span>
              <span>{data.storage_used_mb.toFixed(2)} MB</span>
              <span className="text-muted-foreground">Quota</span>
              <span>{data.storage_quota_mb} MB</span>
              <span className="text-muted-foreground">Documents</span>
              <span>{data.document_count}</span>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
