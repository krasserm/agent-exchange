<script lang="ts">
  import type { Session } from "../lib/types";
  import { statusColor, statusLabel } from "../lib/status";
  import { parentSegments } from "../lib/path";

  let { session, selected, onSelect }: {
    session: Session;
    selected: boolean;
    onSelect: (key: string) => void;
  } = $props();

  // Remote sessions name the machine they run on; local ones show the path alone.
  let parents = $derived(
    parentSegments(session.claude_start_dir) +
      (session.host ? ` (${session.host})` : ""),
  );
  let fullPath = $derived(
    session.host
      ? `${session.host}:${session.claude_start_dir}`
      : session.claude_start_dir,
  );
</script>

<button class="row" class:selected onclick={() => onSelect(session.session_key)}>
  <span class="dot" style="background:{statusColor(session.status)}"></span>
  <span class="meta">
    <span class="path" title={fullPath}>{parents}</span>
    <span class="project">{session.project}</span>
    <span class="sub">
      <span class="st" style="color:{statusColor(session.status)}"
        >{statusLabel(session.status, session.background_tasks + session.session_crons)}</span
      >
    </span>
  </span>
</button>

<style>
  .row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: transparent;
    border: none;
    border-left: 2px solid transparent;
    color: var(--text);
    text-align: left;
    cursor: pointer;
    font: inherit;
    transition:
      background 0.1s ease,
      border-color 0.1s ease;
  }
  .row:hover {
    background: var(--accent-wash);
  }
  .row.selected {
    background: linear-gradient(
      90deg,
      var(--accent-wash),
      transparent 85%
    );
    border-left-color: var(--accent);
    box-shadow: inset 6px 0 14px -10px var(--accent-glow);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    box-shadow: 0 0 7px currentColor;
  }
  .meta {
    display: flex;
    flex-direction: column;
    min-width: 0;
    gap: 0.1rem;
  }
  .path {
    font-size: 0.64rem;
    color: var(--faint);
    letter-spacing: 0.04em;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .project {
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 0.9rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .row.selected .project {
    color: #e6eefb;
  }
  .sub {
    font-size: 0.66rem;
    color: var(--muted);
    display: flex;
    gap: 0.35rem;
    align-items: center;
  }
  .st {
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
</style>
