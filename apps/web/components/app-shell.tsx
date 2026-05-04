"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { TenantSwitcher } from "./tenant-switcher";
import { useAuth } from "@/lib/stores/auth";
import { logout } from "@/lib/api/auth";
import { cn } from "@/lib/utils";
import { FileText, MessageSquare, Network, Settings, LogOut } from "lucide-react";

const NAV = [
  { href: "/documents", label: "Documents", icon: FileText },
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/kg", label: "Knowledge Graph", icon: Network },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const user = useAuth((s) => s.user);
  const clear = useAuth((s) => s.clear);

  const onLogout = async () => {
    await logout();
    clear();
    router.replace("/login");
  };

  return (
    <div className="flex min-h-screen">
      <aside className="w-60 border-r p-4 flex flex-col gap-1">
        <div className="font-semibold text-lg mb-4">RAG-Anything</div>
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = pathname?.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex items-center gap-2 rounded-md px-3 py-2 text-sm hover:bg-accent",
                active && "bg-accent font-medium"
              )}
            >
              <Icon size={16} />
              {label}
            </Link>
          );
        })}
        <div className="mt-auto pt-4 border-t flex flex-col gap-2">
          <div className="text-xs text-muted-foreground">{user?.email}</div>
          <Button variant="ghost" size="sm" onClick={onLogout} className="justify-start gap-2">
            <LogOut size={14} /> Sign out
          </Button>
        </div>
      </aside>
      <div className="flex-1 flex flex-col">
        <header className="h-14 border-b flex items-center justify-end px-4 gap-3">
          <TenantSwitcher />
        </header>
        <main className="flex-1 p-6 overflow-auto">{children}</main>
      </div>
    </div>
  );
}
