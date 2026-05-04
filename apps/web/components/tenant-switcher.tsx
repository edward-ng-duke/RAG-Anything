"use client";
import { useAuth } from "@/lib/stores/auth";
import { selectTenant } from "@/lib/api/auth";
import { toast } from "sonner";

export function TenantSwitcher() {
  const tenants = useAuth((s) => s.tenants);
  const currentId = useAuth((s) => s.currentTenantId);
  const setAccessToken = useAuth((s) => s.setAccessToken);
  const select = useAuth((s) => s.selectTenant);

  if (tenants.length === 0) return null;

  const onChange = async (e: React.ChangeEvent<HTMLSelectElement>) => {
    const tid = e.target.value;
    try {
      const data = await selectTenant(tid);
      setAccessToken(data.access_token);
      select(tid);
    } catch {
      toast.error("failed to switch tenant");
    }
  };

  return (
    <select
      value={currentId ?? ""}
      onChange={onChange}
      className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
    >
      {tenants.map((t) => (
        <option key={t.tenant_id} value={t.tenant_id}>
          {t.display_name}
        </option>
      ))}
    </select>
  );
}
