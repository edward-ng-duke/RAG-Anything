"use client";
import { useState } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuth } from "@/lib/stores/auth";
import { toast } from "sonner";

interface MemberRow {
  user_id: string;
  email: string;
  role: string;
  joined_at: string;
}

// Backend endpoints pending — placeholder data uses current user.
async function listMembers(): Promise<MemberRow[]> {
  throw new Error("/v1/tenants/{id}/members endpoint not yet implemented");
}

export default function MembersSettingsPage() {
  const me = useAuth((s) => s.user);
  const tenantId = useAuth((s) => s.currentTenantId);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin">("member");

  const onInvite = () => {
    if (!inviteEmail) return toast.error("email required");
    toast.info("invite endpoint pending backend support");
  };

  // Render placeholder list with current user only
  const placeholder: MemberRow[] = me
    ? [{ user_id: me.user_id, email: me.email, role: "owner", joined_at: new Date().toISOString() }]
    : [];

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">Members</h2>
      <p className="text-sm text-muted-foreground">
        Tenant: {tenantId || "—"}. Members API pending backend support; the table below
        shows the current user as a placeholder.
      </p>

      <Card>
        <CardHeader><CardTitle className="text-base">Invite</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="role">Role</Label>
            <select
              id="role"
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value as "member" | "admin")}
              className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="member">member</option>
              <option value="admin">admin</option>
            </select>
          </div>
          <Button onClick={onInvite}>Send invite</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">Current members</CardTitle></CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Joined</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {placeholder.map((m) => (
                <TableRow key={m.user_id}>
                  <TableCell>{m.email}</TableCell>
                  <TableCell className="text-muted-foreground">{m.role}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {new Date(m.joined_at).toLocaleDateString()}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => toast.info("remove endpoint pending")}
                      disabled={m.role === "owner"}
                    >
                      Remove
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
