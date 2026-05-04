"use client";
import { useAuth } from "@/lib/stores/auth";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useState } from "react";
import { toast } from "sonner";

export default function ProfilePage() {
  const user = useAuth((s) => s.user);
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [pwdCurrent, setPwdCurrent] = useState("");
  const [pwdNew, setPwdNew] = useState("");

  if (!user) return <p className="text-sm text-muted-foreground">Not signed in.</p>;

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">Profile</h2>

      <Card>
        <CardHeader><CardTitle className="text-base">Account</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div><span className="text-muted-foreground">Email: </span>{user.email}</div>
          <div className="space-y-2">
            <Label htmlFor="dn">Display name</Label>
            <Input
              id="dn"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
            <Button
              size="sm"
              onClick={() => toast.info("display-name update endpoint not yet implemented")}
            >
              Save
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">Change password</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="space-y-2">
            <Label htmlFor="cur">Current password</Label>
            <Input id="cur" type="password" value={pwdCurrent} onChange={(e) => setPwdCurrent(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="new">New password</Label>
            <Input id="new" type="password" value={pwdNew} onChange={(e) => setPwdNew(e.target.value)} />
          </div>
          <Button
            size="sm"
            onClick={() => toast.info("password change endpoint not yet implemented")}
            disabled={!pwdCurrent || pwdNew.length < 8}
          >
            Change password
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
