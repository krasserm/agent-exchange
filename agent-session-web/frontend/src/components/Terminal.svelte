<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { Terminal } from "@xterm/xterm";
  import { FitAddon } from "@xterm/addon-fit";
  import { terminalSocketUrl } from "../lib/api";

  let { sessionKey, mobile = false }: { sessionKey: string; mobile?: boolean } =
    $props();

  let host: HTMLDivElement;
  let term: Terminal | undefined;
  let fit: FitAddon | undefined;
  let ws: WebSocket | undefined;
  let observer: ResizeObserver | undefined;
  let fitTimer: ReturnType<typeof setTimeout> | undefined;
  const encoder = new TextEncoder();

  function sendResize(): void {
    if (!term || ws?.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }

  function doFit(): void {
    try {
      fit?.fit();
    } catch {
      /* element not measurable yet */
    }
  }

  onMount(() => {
    term = new Terminal({
      cursorBlink: true,
      fontFamily:
        "'JetBrains Mono', Menlo, Monaco, 'Courier New', monospace",
      // Smaller on phones so more of the 80-col TUI fits when fitting to width.
      fontSize: mobile ? 11 : 13,
      theme: {
        background: "#070b12",
        foreground: "#c9d6e5",
        cursor: "#22d3ee",
        cursorAccent: "#070b12",
        selectionBackground: "rgba(34,211,238,0.28)",
        black: "#0b1320",
        brightBlack: "#44566b",
      },
    });
    fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    doFit();

    ws = new WebSocket(terminalSocketUrl(sessionKey));
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      doFit();
      sendResize();
      term?.focus();
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (ev.data instanceof ArrayBuffer) {
        term?.write(new Uint8Array(ev.data));
      }
    };
    ws.onclose = () => {
      term?.write("\r\n\x1b[2m[disconnected]\x1b[0m\r\n");
    };

    term.onData((data: string) => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(encoder.encode(data));
      }
    });
    term.onResize(() => sendResize());

    observer = new ResizeObserver(() => {
      if (fitTimer) clearTimeout(fitTimer);
      fitTimer = setTimeout(doFit, 80);
    });
    observer.observe(host);
  });

  onDestroy(() => {
    if (fitTimer) clearTimeout(fitTimer);
    observer?.disconnect();
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
    term?.dispose();
  });
</script>

<div class="terminal" bind:this={host}></div>

<style>
  .terminal {
    flex: 1;
    min-height: 0;
    padding: 0.5rem 0.6rem;
    background: #070b12;
    overflow: hidden;
  }
</style>
