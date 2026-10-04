"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { LogOut, MessageSquare, Sprout } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApiKey } from "@/lib/api-key";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Studies" },
  { href: "/studies/new", label: "New study" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { clearApiKey, devMode } = useApiKey();
  return (
    <div className="bg-muted/30 flex min-h-screen flex-col">
      <header className="bg-background sticky top-0 z-30 border-b print:hidden">
        <div className="mx-auto flex h-14 w-full max-w-[1600px] items-center gap-4 px-4">
          <Link
            href="/"
            className="flex items-center gap-2 font-semibold tracking-tight"
          >
            <Sprout className="size-5 text-emerald-700" />
            AgriHub
          </Link>
          <nav
            aria-label="Main"
            className="flex items-center gap-1"
          >
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={pathname === item.href ? "page" : undefined}
                className={cn(
                  "text-muted-foreground hover:text-foreground rounded-md px-3 py-1.5 text-sm",
                  pathname === item.href && "bg-muted text-foreground",
                )}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            {devMode && (
              <span className="text-muted-foreground hidden text-xs sm:inline">
                Development mode
              </span>
            )}
            <Button
              asChild
              variant="ghost"
              size="sm"
            >
              <Link href="/chat">
                <MessageSquare />
                Chat
              </Link>
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => clearApiKey()}
            >
              <LogOut />
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto flex w-full max-w-[1600px] flex-1 flex-col px-4 py-6">
        {children}
      </main>
    </div>
  );
}
