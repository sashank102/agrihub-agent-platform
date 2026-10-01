import { Badge } from "@/components/ui/badge";

const LABELS: Record<
  string,
  {
    label: string;
    variant: "success" | "warning" | "destructive" | "info" | "outline";
  }
> = {
  pending: { label: "Queued", variant: "info" },
  running: { label: "Running", variant: "info" },
  completed: { label: "Completed", variant: "success" },
  failed: { label: "Failed", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "warning" },
  interrupted: { label: "Interrupted", variant: "warning" },
  idle: { label: "Idle", variant: "outline" },
};

export function RunStatusBadge({ status }: { status: string }) {
  const entry = LABELS[status] ?? { label: status, variant: "outline" };
  return <Badge variant={entry.variant}>{entry.label}</Badge>;
}
