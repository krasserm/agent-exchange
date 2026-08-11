<script lang="ts">
  // A thin vertical drag handle that reports incremental horizontal movement.
  // `sign` flips the delta so a handle on the panel's right vs left edge both
  // feel natural (drag right = grow the panel it belongs to).
  let { onResize, sign = 1 }: { onResize: (dx: number) => void; sign?: number } =
    $props();

  let dragging = $state(false);
  let lastX = 0;

  function down(e: PointerEvent): void {
    dragging = true;
    lastX = e.clientX;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function move(e: PointerEvent): void {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    lastX = e.clientX;
    if (dx !== 0) onResize(dx * sign);
  }

  function up(e: PointerEvent): void {
    dragging = false;
    try {
      (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* capture already gone */
    }
  }
</script>

<div
  class="resizer"
  class:dragging
  role="separator"
  aria-orientation="vertical"
  onpointerdown={down}
  onpointermove={move}
  onpointerup={up}
  onpointercancel={up}
>
  <span class="grip"></span>
</div>

<style>
  .resizer {
    flex: 0 0 6px;
    width: 6px;
    cursor: col-resize;
    position: relative;
    z-index: 5;
    background: transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    touch-action: none;
  }
  .resizer::before {
    /* the seam line */
    content: "";
    position: absolute;
    inset: 0 2px;
    background: var(--line);
    transition: background 0.12s ease;
  }
  .grip {
    position: relative;
    width: 4px;
    height: 34px;
    border-radius: 2px;
    /* a stack of HUD ticks rather than a plain bar */
    background-image: repeating-linear-gradient(
      0deg,
      var(--faint) 0,
      var(--faint) 2px,
      transparent 2px,
      transparent 5px
    );
    transition:
      background-image 0.12s ease,
      box-shadow 0.12s ease,
      height 0.12s ease;
  }
  .resizer:hover::before,
  .resizer.dragging::before {
    background: var(--accent-deep);
  }
  .resizer:hover .grip,
  .resizer.dragging .grip {
    height: 54px;
    background-image: repeating-linear-gradient(
      0deg,
      var(--accent) 0,
      var(--accent) 2px,
      transparent 2px,
      transparent 5px
    );
    box-shadow: 0 0 12px var(--accent-glow);
  }
</style>
