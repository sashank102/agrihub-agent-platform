"use client";

import React from "react";
import { AppShell } from "@/components/study/app-shell";
import { Toaster } from "@/components/ui/sonner";
import { ApiKeyProvider } from "@/lib/api-key";
import { StudyStreamProvider } from "@/providers/StudyStream";

export default function StudiesLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <React.Suspense fallback={<div className="min-h-screen" />}>
      <Toaster />
      <ApiKeyProvider>
        <StudyStreamProvider>
          <AppShell>{children}</AppShell>
        </StudyStreamProvider>
      </ApiKeyProvider>
    </React.Suspense>
  );
}
