import client from "./client";

export interface TenantInfo {
  tenant_id: string;
  display_name: string;
  storage_quota_mb: number;
  storage_used_mb: number;
  document_count: number;
}

export async function getCurrentTenant() {
  const r = await client.get<TenantInfo>("/v1/tenants/me");
  return r.data;
}

// PATCH endpoint not yet implemented backend-side — leave a stub that toasts.
export async function updateTenantConfig(_payload: { display_name?: string; llm?: any }) {
  // Backend: PATCH /v1/tenants/{tenant_id} (TODO)
  throw new Error("update endpoint not yet implemented");
}
