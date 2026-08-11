<script lang="ts">
  import { getMessages } from "../lib/api";
  import type { Message } from "../lib/types";

  let { sessionKey }: { sessionKey: string } = $props();

  let messages = $state<Message[]>([]);

  // Keep the newest response in view ("tail" behaviour), unless the user has
  // scrolled up to read history — then leave their position alone.
  let streamEl: HTMLDivElement | undefined;
  let pinBottom = true;
  function onScroll(): void {
    const el = streamEl;
    if (el) pinBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }
  $effect(() => {
    messages;
    if (pinBottom && streamEl) streamEl.scrollTop = streamEl.scrollHeight;
  });

  // How many recent responses to show. "all" maps to the daemon's 50-msg buffer.
  const CHOICES = ["2", "5", "10", "all"] as const;
  type Choice = (typeof CHOICES)[number];
  let count = $state<Choice>(readCount());

  function readCount(): Choice {
    const v = localStorage.getItem("csw.msgCount");
    return (CHOICES as readonly string[]).includes(v ?? "")
      ? (v as Choice)
      : "2";
  }
  function fetchN(): number {
    return count === "all" ? 50 : Number(count);
  }
  function setCount(c: Choice): void {
    if (c === count) return;
    count = c;
    localStorage.setItem("csw.msgCount", c);
    void refresh();
  }

  function same(a: Message[], b: Message[]): boolean {
    return (
      a.length === b.length &&
      a.every(
        (m, i) => m.timestamp === b[i].timestamp && m.message === b[i].message,
      )
    );
  }

  async function refresh(): Promise<void> {
    // `asn messages --last N` returns oldest -> newest; keep that order so the
    // latest sits at the bottom (transcript style), older collapsed ones above.
    const next = await getMessages(sessionKey, fetchN());
    // Only reassign on real change so the poll never collapses a card the
    // user expanded by hand (re-render would re-assert the default open state).
    if (!same(next, messages)) messages = next;
  }

  // Short clock form (HH:MM:SS) for the HUD; falls back to the raw stamp.
  function clock(ts: string): string {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleTimeString([], { hour12: false });
  }

  function preview(text: string): string {
    const line = text.replace(/\s+/g, " ").trim();
    return line.length > 64 ? line.slice(0, 64) + "…" : line;
  }

  // Refetch on session change, then poll so responses typed straight into the
  // tmux pane surface here automatically (no composer to trigger a refetch).
  $effect(() => {
    sessionKey;
    void refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  });
</script>

<section class="panel">
  <header class="head">
    <span class="hud-head">▸ RESPONSES</span>
    <div class="head-right">
      <div class="seg" role="group" aria-label="Number of responses">
        {#each CHOICES as c}
          <button
            class:active={count === c}
            onclick={() => setCount(c)}
            title="Show last {c} responses"
          >
            {c === "all" ? "ALL" : c}
          </button>
        {/each}
      </div>
      <button class="refresh" onclick={refresh} title="Reload responses">↻</button>
    </div>
  </header>

  <div class="stream" bind:this={streamEl} onscroll={onScroll}>
    {#if messages.length === 0}
      <p class="empty">// no assistant output yet</p>
    {:else}
      {#each messages as m, i (m.timestamp)}
        {@const latest = i === messages.length - 1}
        <details class="msg" class:latest open={latest}>
          <summary>
            <span class="caret"></span>
            <span class="tag">{latest ? "LATEST" : "PREV"}</span>
            <time>{clock(m.timestamp)}</time>
            {#if !latest}
              <span class="peek">{preview(m.message)}</span>
            {/if}
          </summary>
          <pre>{m.message}</pre>
        </details>
      {/each}
      <div class="terminator"><span>// end of stream</span></div>
    {/if}
  </div>
</section>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: linear-gradient(180deg, var(--panel), var(--bg-deep));
    border-left: 1px solid var(--line);
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.6rem 0.75rem;
    border-bottom: 1px solid var(--line);
    background: var(--panel-2);
  }
  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .seg {
    display: flex;
    border: 1px solid var(--line);
    border-radius: 3px;
    overflow: hidden;
  }
  .seg button {
    background: transparent;
    border: none;
    border-left: 1px solid var(--line);
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    padding: 0.16rem 0.4rem;
    cursor: pointer;
    transition:
      color 0.12s ease,
      background 0.12s ease;
  }
  .seg button:first-child {
    border-left: none;
  }
  .seg button:hover {
    color: var(--text);
  }
  .seg button.active {
    color: var(--accent);
    background: var(--accent-wash);
    text-shadow: 0 0 8px var(--accent-glow);
  }
  .refresh {
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.95rem;
    line-height: 1;
    transition:
      color 0.12s ease,
      text-shadow 0.12s ease,
      transform 0.2s ease;
  }
  .refresh:hover {
    color: var(--accent);
    text-shadow: 0 0 10px var(--accent-glow);
    transform: rotate(90deg);
  }
  .stream {
    flex: 1;
    overflow-y: auto;
    padding: 0.6rem 0.6rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .empty {
    color: var(--faint);
    font-size: 0.8rem;
    padding: 0.4rem 0.2rem;
  }

  .msg {
    border: 1px solid var(--line);
    border-radius: 4px;
    background: var(--panel);
    overflow: hidden;
  }
  .msg.latest {
    border-color: var(--accent-deep);
    box-shadow: 0 0 0 1px var(--accent-deep), 0 0 18px rgba(34, 211, 238, 0.06);
  }
  /* The expanded latest card grows into spare space (so it fills the panel when
     there are only a few responses) but never shrinks below its own content, so
     a long collapsed history just makes the whole stream scroll instead of
     squashing the latest message. */
  .msg.latest[open] {
    flex: 1 0 auto;
  }
  summary {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.55rem;
    cursor: pointer;
    list-style: none;
    user-select: none;
    background: var(--panel-2);
  }
  summary::-webkit-details-marker {
    display: none;
  }
  .msg.latest summary {
    background: color-mix(in srgb, var(--accent-deep) 35%, var(--panel-2));
  }
  .msg:not(.latest) summary:hover {
    background: var(--panel-hi);
  }
  .msg:not(.latest) summary:hover .caret {
    border-left-color: var(--accent);
  }
  .msg:not(.latest) summary:hover time,
  .msg:not(.latest) summary:hover .peek {
    color: var(--text);
  }
  .caret {
    width: 0;
    height: 0;
    border-left: 5px solid var(--muted);
    border-top: 4px solid transparent;
    border-bottom: 4px solid transparent;
    transition: transform 0.15s ease;
    flex-shrink: 0;
  }
  .msg[open] .caret {
    transform: rotate(90deg);
    border-left-color: var(--accent);
  }
  .tag {
    font-family: var(--font-display);
    font-size: 0.58rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    color: var(--accent);
    padding: 0.05rem 0.32rem;
    border: 1px solid var(--accent-deep);
    border-radius: 2px;
    flex-shrink: 0;
  }
  .msg:not(.latest) .tag {
    color: var(--muted);
    border-color: var(--line);
  }
  time {
    font-size: 0.68rem;
    color: var(--accent);
    letter-spacing: 0.04em;
    text-shadow: 0 0 8px rgba(34, 211, 238, 0.35);
    flex-shrink: 0;
  }
  .msg:not(.latest) time {
    color: var(--muted);
    text-shadow: none;
  }
  .peek {
    font-size: 0.68rem;
    color: var(--faint);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .terminator {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.1rem 0.15rem;
    color: var(--faint);
    font-size: 0.58rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    flex: 0 0 auto;
  }
  .terminator::before,
  .terminator::after {
    content: "";
    height: 1px;
    flex: 1;
    background: linear-gradient(90deg, transparent, var(--line), transparent);
  }
  pre {
    margin: 0;
    padding: 0.6rem 0.7rem;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    line-height: 1.55;
    color: var(--text);
    border-top: 1px solid var(--line-soft);
  }
</style>
