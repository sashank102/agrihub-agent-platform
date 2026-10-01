export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) {
    return "—";
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  const seconds = ms / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
}

export function between(
  start: string | null | undefined,
  end: string | null | undefined,
): number | null {
  if (!start || !end) {
    return null;
  }
  return Date.parse(end) - Date.parse(start);
}

export function formatBp(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "—"
    : value.toLocaleString("en-US");
}

export function formatDistance(
  distance: number | null | undefined,
  overlaps: boolean | undefined,
): string {
  if (overlaps) {
    return "overlaps SNP";
  }
  if (distance === null || distance === undefined) {
    return "—";
  }
  return distance < 1000
    ? `${distance} bp`
    : `${(distance / 1000).toFixed(1)} kb`;
}

export function formatTokens(count: number): string {
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}
