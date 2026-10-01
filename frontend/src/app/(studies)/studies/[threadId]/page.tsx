"use client";

import { use } from "react";
import { StudyRunPage } from "@/components/study/run/study-run-page";

export default function StudyPage({
  params,
}: {
  params: Promise<{ threadId: string }>;
}) {
  const { threadId } = use(params);
  return (
    <StudyRunPage
      key={threadId}
      threadId={threadId}
    />
  );
}
