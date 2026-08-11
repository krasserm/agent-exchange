export function statusLabel(status: string | null, backgroundCount = 0): string {
  const label = status ?? "ended";
  return backgroundCount > 0 ? `${label} (${backgroundCount})` : label;
}

// Colors mirror asn-top semantics, tuned for the HUD palette.
export function statusColor(status: string | null): string {
  switch (status) {
    case "idle":
      return "#38bdf8"; // sky = idle/online
    case "working":
      return "#34d399"; // green = working
    case "waiting":
      return "#f59e0b"; // amber = waiting
    case "backgrounded":
      return "#a78bfa"; // violet = alive, paused on background work
    case "disconnected":
      return "#fb7185"; // rose = proxy lost, session may still be alive
    default:
      return "#475569"; // slate = ended
  }
}

// A single-glyph instrument marker per status.
export function statusGlyph(status: string | null): string {
  switch (status) {
    case "working":
      return "▰";
    case "waiting":
      return "◆";
    case "idle":
      return "●";
    case "backgrounded":
      return "◐";
    case "disconnected":
      return "◇";
    default:
      return "○";
  }
}
