<script lang="ts">
  import SessionList from "./components/SessionList.svelte";
  import Terminal from "./components/Terminal.svelte";
  import MessagesPanel from "./components/MessagesPanel.svelte";
  import Resizer from "./components/Resizer.svelte";
  import { sessions, selectedKey } from "./lib/stores";
  import { stopSession, listSessions } from "./lib/api";
  import { statusColor, statusLabel } from "./lib/status";
  import type { Session } from "./lib/types";

  let items = $state<Session[]>([]);
  let key = $state<string | null>(null);

  sessions.subscribe((v) => (items = v));
  selectedKey.subscribe((v) => (key = v));

  let selected = $derived(items.find((s) => s.session_key === key) ?? null);
  let ended = $derived(selected != null && selected.status == null);

  // Resizable pane widths (px), persisted across reloads. Desktop only.
  const NAV_MIN = 180,
    NAV_MAX = 520,
    MSG_MIN = 240,
    MSG_MAX = 760;
  const clamp = (v: number, lo: number, hi: number) =>
    Math.min(hi, Math.max(lo, v));
  const readW = (k: string, d: number) => {
    const v = Number(localStorage.getItem(k));
    return Number.isFinite(v) && v > 0 ? v : d;
  };

  let navWidth = $state(readW("csw.navW", 264));
  let msgWidth = $state(readW("csw.msgW", 360));

  function resizeNav(dx: number): void {
    navWidth = clamp(navWidth + dx, NAV_MIN, NAV_MAX);
    localStorage.setItem("csw.navW", String(navWidth));
  }
  function resizeMsg(dx: number): void {
    msgWidth = clamp(msgWidth + dx, MSG_MIN, MSG_MAX);
    localStorage.setItem("csw.msgW", String(msgWidth));
  }

  // Desktop: the responses panel is a drawer hidden by default; a status-bar
  // button slides it in from the right. State persists across reloads.
  let panelOpen = $state(localStorage.getItem("csw.panelOpen") === "1");
  function togglePanel(): void {
    panelOpen = !panelOpen;
    localStorage.setItem("csw.panelOpen", panelOpen ? "1" : "0");
  }

  // Mobile: single column with a bottom tab bar instead of side-by-side panes.
  let mobile = $state(false);
  let tab = $state<"sessions" | "terminal" | "responses">("sessions");

  $effect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const apply = () => (mobile = mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  });

  // Jump to the terminal once a session is picked from the roster on mobile.
  function pick(): void {
    if (mobile) tab = "terminal";
  }

  async function stop(): Promise<void> {
    if (!key) return;
    await stopSession(key);
    sessions.set(await listSessions());
  }
</script>

{#snippet statusBar()}
  <header class="status">
    <span class="dot" style="background:{statusColor(selected!.status)}"></span>
    <span class="title">{selected!.project}</span>
    <span class="badge" style="--c:{statusColor(selected!.status)}">
      [ {statusLabel(selected!.status, selected!.background_tasks + selected!.session_crons)} ]
    </span>
    <span class="tmux">{selected!.tmux_session_name ?? ""}</span>
    <span class="spacer"></span>
    <span class="key">{selected!.session_key.slice(0, 12)}</span>
    {#if !mobile}
      <button
        class="ghost"
        class:active={panelOpen}
        onclick={togglePanel}
        title="Toggle responses panel"
      >
        ▤ RESPONSES
      </button>
    {/if}
    <button class="stop" onclick={stop} disabled={ended}>■ STOP</button>
  </header>
{/snippet}

{#snippet emptyState()}
  <div class="placeholder">
    <span class="ph-tag"><span class="ph-tag-dot"></span>STANDBY</span>
    <div class="reticle">
      <span class="r-ring"></span>
      <span class="r-cross"></span>
      <span class="r-dot"></span>
    </div>
    <span class="ph-line">NO SESSION ATTACHED</span>
    <span class="ph-sub">// select a session from the roster to attach its terminal</span>
    <span class="ph-meta"
      >{items.length} session{items.length === 1 ? "" : "s"} on roster</span
    >
  </div>
{/snippet}

{#snippet noSession()}
  <div class="placeholder">
    <span class="ph-line">NO SESSION ATTACHED</span>
    <span class="ph-sub">// open the Sessions tab to choose one</span>
  </div>
{/snippet}

{#if mobile}
  <div class="app mobile">
    {#if selected && tab !== "sessions"}
      {@render statusBar()}
    {/if}

    <div class="m-view">
      <div class="m-pane" class:active={tab === "sessions"}>
        <SessionList onpick={pick} />
      </div>

      <div class="m-pane" class:active={tab === "terminal"}>
        {#if selected}
          {#key key}
            {#if !ended}
              <Terminal sessionKey={key!} mobile />
            {:else}
              <div class="gone">SESSION TERMINATED</div>
            {/if}
          {/key}
        {:else}
          {@render noSession()}
        {/if}
      </div>

      <div class="m-pane" class:active={tab === "responses"}>
        {#if selected}
          {#key key}
            <MessagesPanel sessionKey={key!} />
          {/key}
        {:else}
          {@render noSession()}
        {/if}
      </div>
    </div>

    <nav class="tabbar">
      <button class:active={tab === "sessions"} onclick={() => (tab = "sessions")}>
        <span class="tb-glyph">▤</span>
        <span class="tb-label">Sessions</span>
        {#if items.length}<span class="tb-count">{items.length}</span>{/if}
      </button>
      <button class:active={tab === "terminal"} onclick={() => (tab = "terminal")}>
        <span class="tb-glyph">❯_</span>
        <span class="tb-label">Terminal</span>
        {#if selected}<span
            class="tb-dot"
            style="background:{statusColor(selected.status)}"
          ></span>{/if}
      </button>
      <button
        class:active={tab === "responses"}
        onclick={() => (tab = "responses")}
      >
        <span class="tb-glyph">✦</span>
        <span class="tb-label">Responses</span>
      </button>
    </nav>
  </div>
{:else}
  <div class="app">
    <aside class="nav" style="width:{navWidth}px">
      <SessionList />
    </aside>
    <Resizer onResize={resizeNav} sign={1} />

    <main class="main">
      {#if selected}
        {@render statusBar()}

        <div class="content">
          <div class="term-wrap">
            {#key key}
              {#if !ended}
                <Terminal sessionKey={key!} />
              {:else}
                <div class="gone">SESSION TERMINATED</div>
              {/if}
            {/key}
          </div>
          <aside class="drawer" class:open={panelOpen} style="width:{msgWidth}px">
            <Resizer onResize={resizeMsg} sign={-1} />
            <div class="msg-host">
              {#key key}
                <MessagesPanel sessionKey={key!} />
              {/key}
            </div>
          </aside>
        </div>
      {:else}
        {@render emptyState()}
      {/if}
    </main>
  </div>
{/if}

<style>
  .app {
    display: flex;
    height: 100vh;
    width: 100vw;
    overflow: hidden;
  }
  .nav {
    flex: 0 0 auto;
    min-height: 0;
    min-width: 0;
  }
  .main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    min-height: 0;
    background: var(--bg);
  }
  .status {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.55rem 0.9rem;
    border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    position: relative;
  }
  .status::after {
    /* hairline accent under the instrument bar */
    content: "";
    position: absolute;
    left: 0;
    right: 0;
    bottom: -1px;
    height: 1px;
    background: linear-gradient(
      90deg,
      transparent,
      var(--accent-dim) 18%,
      transparent 70%
    );
    opacity: 0.6;
  }
  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    box-shadow: 0 0 8px currentColor;
    flex-shrink: 0;
  }
  .title {
    font-family: var(--font-display);
    font-weight: 700;
    font-size: 1.02rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: #e9f1fb;
    white-space: nowrap;
  }
  .badge {
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    color: var(--c, var(--accent));
    border: 1px solid color-mix(in srgb, var(--c, var(--accent)) 45%, transparent);
    background: color-mix(in srgb, var(--c, var(--accent)) 12%, transparent);
    border-radius: 3px;
    padding: 0.12rem 0.42rem;
    text-shadow: 0 0 8px color-mix(in srgb, var(--c, var(--accent)) 45%, transparent);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .tmux {
    font-size: 0.7rem;
    color: var(--faint);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 34ch;
  }
  .key {
    font-size: 0.7rem;
    color: var(--muted);
    letter-spacing: 0.05em;
  }
  .spacer {
    flex: 1;
  }
  .stop {
    font-family: var(--font-mono);
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent);
    color: var(--danger);
    padding: 0.32rem 0.7rem;
    border-radius: 3px;
    cursor: pointer;
    flex-shrink: 0;
    transition:
      background 0.12s ease,
      box-shadow 0.12s ease;
  }
  .stop:hover:not(:disabled) {
    background: color-mix(in srgb, var(--danger) 16%, transparent);
    box-shadow: 0 0 12px color-mix(in srgb, var(--danger) 40%, transparent);
  }
  .stop:disabled {
    border-color: var(--line);
    color: var(--faint);
    cursor: not-allowed;
  }
  .content {
    display: flex;
    flex: 1;
    min-height: 0;
    position: relative;
    overflow: hidden;
  }
  .term-wrap {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    min-height: 0;
  }
  /* Responses drawer: overlays the right edge, slides in when open. */
  .drawer {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    display: flex;
    min-height: 0;
    transform: translateX(100%);
    transition: transform 0.26s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: -22px 0 46px -24px rgba(0, 0, 0, 0.8);
    z-index: 6;
  }
  .drawer.open {
    transform: translateX(0);
  }
  .msg-host {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
  }
  .ghost {
    font-family: var(--font-mono);
    font-size: 0.64rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    background: transparent;
    border: 1px solid var(--line);
    color: var(--muted);
    padding: 0.32rem 0.6rem;
    border-radius: 3px;
    cursor: pointer;
    flex-shrink: 0;
    transition:
      color 0.12s ease,
      border-color 0.12s ease,
      box-shadow 0.12s ease,
      background 0.12s ease;
  }
  .ghost:hover {
    color: var(--text);
    border-color: var(--accent-deep);
  }
  .ghost.active {
    color: var(--accent);
    border-color: var(--accent-deep);
    background: var(--accent-wash);
    box-shadow: 0 0 12px -2px var(--accent-glow);
  }
  .gone,
  .placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    height: 100%;
    color: var(--muted);
    font-family: var(--font-display);
    letter-spacing: var(--hud-track);
    font-size: 0.8rem;
    text-align: center;
    padding: 1rem;
  }
  .ph-line {
    color: var(--text);
    text-shadow: 0 0 12px rgba(34, 211, 238, 0.12);
  }
  .ph-sub {
    font-family: var(--font-mono);
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    color: var(--faint);
    text-transform: none;
  }
  .ph-tag {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.6rem;
    letter-spacing: 0.24em;
    color: var(--accent);
    border: 1px solid var(--accent-deep);
    background: var(--accent-wash);
    border-radius: 3px;
    padding: 0.18rem 0.55rem;
    margin-bottom: 1.2rem;
  }
  .ph-tag-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 8px var(--accent-glow);
    animation: blink 1.6s ease-in-out infinite;
  }
  @keyframes blink {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.2;
    }
  }
  .ph-meta {
    margin-top: 0.9rem;
    font-family: var(--font-mono);
    font-size: 0.62rem;
    letter-spacing: 0.1em;
    color: var(--faint);
    text-transform: uppercase;
  }
  .reticle {
    position: relative;
    width: 88px;
    height: 88px;
    margin-bottom: 0.7rem;
    opacity: 0.85;
  }
  .reticle .r-ring {
    position: absolute;
    inset: 0;
    border: 1px solid var(--accent-deep);
    border-radius: 50%;
    box-shadow:
      0 0 18px rgba(34, 211, 238, 0.12),
      inset 0 0 18px rgba(34, 211, 238, 0.06);
    animation: spin 14s linear infinite;
    border-top-color: var(--accent);
    border-right-color: transparent;
  }
  .reticle .r-cross::before,
  .reticle .r-cross::after {
    content: "";
    position: absolute;
    background: var(--accent-deep);
  }
  .reticle .r-cross::before {
    left: 50%;
    top: 12px;
    bottom: 12px;
    width: 1px;
    transform: translateX(-0.5px);
  }
  .reticle .r-cross::after {
    top: 50%;
    left: 12px;
    right: 12px;
    height: 1px;
    transform: translateY(-0.5px);
  }
  .reticle .r-dot {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    transform: translate(-50%, -50%);
    box-shadow: 0 0 10px var(--accent-glow);
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* ---- Mobile: single column + bottom tab bar ---- */
  .app.mobile {
    flex-direction: column;
    overflow: hidden;
  }
  .app.mobile .status {
    padding: 0.5rem 0.75rem;
  }
  .app.mobile .tmux,
  .app.mobile .key {
    display: none;
  }
  .m-view {
    flex: 1;
    min-height: 0;
    min-width: 0;
    display: flex;
    position: relative;
  }
  .m-pane {
    display: none;
    flex: 1;
    min-width: 0;
    min-height: 0;
  }
  .m-pane.active {
    display: flex;
    flex-direction: column;
  }
  .tabbar {
    display: flex;
    flex: 0 0 auto;
    border-top: 1px solid var(--line);
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    padding-bottom: env(safe-area-inset-bottom, 0);
  }
  .tabbar button {
    position: relative;
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.18rem;
    padding: 0.5rem 0.25rem 0.55rem;
    background: transparent;
    border: none;
    border-top: 2px solid transparent;
    color: var(--muted);
    font: inherit;
    cursor: pointer;
    transition:
      color 0.12s ease,
      border-color 0.12s ease;
  }
  .tabbar button.active {
    color: var(--accent);
    border-top-color: var(--accent);
    background: var(--accent-wash);
  }
  .tb-glyph {
    font-size: 0.95rem;
    line-height: 1;
    text-shadow: 0 0 8px transparent;
  }
  .tabbar button.active .tb-glyph {
    text-shadow: 0 0 10px var(--accent-glow);
  }
  .tb-label {
    font-family: var(--font-display);
    font-size: 0.6rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .tb-count {
    position: absolute;
    top: 0.3rem;
    right: 50%;
    transform: translateX(1.6rem);
    font-size: 0.55rem;
    color: var(--accent);
    background: var(--accent-deep);
    border-radius: 6px;
    padding: 0 0.28rem;
  }
  .tb-dot {
    position: absolute;
    top: 0.42rem;
    right: 50%;
    transform: translateX(1.5rem);
    width: 6px;
    height: 6px;
    border-radius: 50%;
    box-shadow: 0 0 6px currentColor;
  }
</style>
