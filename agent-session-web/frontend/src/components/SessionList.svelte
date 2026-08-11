<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { listSessions } from "../lib/api";
  import { sessions, selectedKey } from "../lib/stores";
  import SessionRow from "./SessionRow.svelte";
  import type { Session } from "../lib/types";

  // `onpick` lets the parent react to a selection (mobile: switch to terminal).
  let { onpick }: { onpick?: () => void } = $props();

  let items = $state<Session[]>([]);
  let current = $state<string | null>(null);
  let timer: ReturnType<typeof setInterval> | undefined;

  const unsubSessions = sessions.subscribe((v) => (items = v));
  const unsubSelected = selectedKey.subscribe((v) => (current = v));

  async function refresh(): Promise<void> {
    sessions.set(await listSessions());
  }

  function select(key: string): void {
    selectedKey.set(key);
    onpick?.();
  }

  onMount(() => {
    void refresh();
    timer = setInterval(refresh, 1000);
  });

  onDestroy(() => {
    if (timer) clearInterval(timer);
    unsubSessions();
    unsubSelected();
  });
</script>

<nav class="list">
  <header class="head">
    <span class="hud-head">▸ SESSIONS</span>
    <span class="count">{items.length.toString().padStart(2, "0")}</span>
  </header>
  <div class="rows">
    {#if items.length === 0}
      <p class="empty">// no active sessions</p>
    {:else}
      {#each items as session (session.session_key)}
        <SessionRow
          {session}
          selected={session.session_key === current}
          onSelect={select}
        />
      {/each}
    {/if}
  </div>
  <footer class="foot">
    <span class="live"></span>
    <span>asn · live poll 1s</span>
  </footer>
</nav>

<style>
  .list {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: linear-gradient(180deg, var(--panel), var(--bg-deep));
    border-right: 1px solid var(--line);
  }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.7rem 0.75rem;
    border-bottom: 1px solid var(--line);
    background: var(--panel-2);
  }
  .count {
    font-family: var(--font-display);
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--accent);
    text-shadow: 0 0 9px var(--accent-glow);
  }
  .rows {
    flex: 1;
    overflow-y: auto;
    padding: 0.35rem 0;
  }
  .empty {
    padding: 1rem 0.75rem;
    color: var(--faint);
    font-size: 0.78rem;
  }
  .foot {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.5rem 0.75rem;
    border-top: 1px solid var(--line);
    font-size: 0.62rem;
    letter-spacing: 0.08em;
    color: var(--faint);
    text-transform: uppercase;
  }
  .live {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ok);
    box-shadow: 0 0 8px var(--ok);
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.25;
    }
  }
</style>
