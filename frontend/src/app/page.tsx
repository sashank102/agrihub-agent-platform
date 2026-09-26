"use client";

import { Thread } from "@/components/thread";
import { ApiKeyProvider } from "@/lib/api-key";
import { TenantSessionReset } from "@/lib/tenant-state";
import { StreamProvider } from "@/providers/Stream";
import { ThreadProvider } from "@/providers/Thread";
import { ArtifactProvider } from "@/components/thread/artifact";
import { Toaster } from "@/components/ui/sonner";
import React from "react";

export default function DemoPage(): React.ReactNode {
  return (
    <React.Suspense fallback={<div>Loading (layout)...</div>}>
      <Toaster />
      <ApiKeyProvider>
        <ThreadProvider>
          <TenantSessionReset />
          <StreamProvider>
            <ArtifactProvider>
              <Thread />
            </ArtifactProvider>
          </StreamProvider>
        </ThreadProvider>
      </ApiKeyProvider>
    </React.Suspense>
  );
}
