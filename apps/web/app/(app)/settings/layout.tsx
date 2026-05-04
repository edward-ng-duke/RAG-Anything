"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/settings/profile", label: "Profile" },
  { href: "/settings/tenant", label: "Tenant" },
  { href: "/settings/llm", label: "LLM" },
  { href: "/settings/members", label: "Members" },
  { href: "/settings/api-keys", label: "API keys" },
];

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="grid grid-cols-[200px_1fr] gap-6">
      <aside className="space-y-1">
        <h1 className="text-lg font-semibold mb-2">Settings</h1>
        {NAV.map(({ href, label }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "block rounded-md px-3 py-1.5 text-sm hover:bg-accent",
                active && "bg-accent font-medium"
              )}
            >
              {label}
            </Link>
          );
        })}
      </aside>
      <main className="min-w-0">{children}</main>
    </div>
  );
}
