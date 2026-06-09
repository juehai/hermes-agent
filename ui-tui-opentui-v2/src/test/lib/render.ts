/**
 * test/lib/render.ts — headless renderable verification helpers (spec v4 §5
 * Layer 2). Wraps the Solid binding's `testRender` + the settle dance.
 *
 * Settling needs care: Solid mounts async; a `<scrollbox>` needs a couple of
 * passes to measure content + apply stickyStart; and the native `<markdown>`
 * (Tree-sitter) tokenizes ASYNCHRONOUSLY — a plain `renderOnce` loop captures
 * before its text paints. So we `flush()` (wait until scheduled rendering
 * settles) between passes, and `captureFrame` can wait for specific content via
 * `until` (retries with `waitForFrame`) for markdown-bearing frames.
 *
 * `exitOnCtrlC: false` is forced (gotcha §8 #7 — the test renderer defaults true
 * and would tear down on the first simulated Ctrl+C, blanking later frames).
 *
 * Keymap (Phase 3): overlays/prompts register close layers via `@opentui/keymap`,
 * whose hooks throw without a `<KeymapProvider>`. The entry provides one in the
 * real app; here we provide a test keymap built from the test renderer (read via
 * `useRenderer()` inside the tree) so headless mounts of those views work.
 */
import { createDefaultOpenTuiKeymap } from '@opentui/keymap/opentui'
import { KeymapProvider } from '@opentui/keymap/solid'
import { testRender, useRenderer } from '@opentui/solid'
import type { JSX } from '@opentui/solid'
import { createMemo } from 'solid-js'

/** Wrap a node in a KeymapProvider whose keymap is bound to the test renderer. */
function withKeymap(node: () => JSX.Element): () => JSX.Element {
  return () => {
    const renderer = useRenderer()
    const keymap = createMemo(() => createDefaultOpenTuiKeymap(renderer))
    return KeymapProvider({
      keymap: keymap(),
      get children() {
        return node()
      }
    })
  }
}

export interface RenderProbe {
  readonly frame: () => string
  readonly waitForFrame: (predicate: (frame: string) => boolean) => Promise<string>
  readonly resize: (width: number, height: number) => void
  readonly destroy: () => void
}

/** Mount a Solid node headlessly and return a probe with a settled first frame. */
export async function renderProbe(
  node: () => JSX.Element,
  options?: { width?: number; height?: number }
): Promise<RenderProbe> {
  const setup = await testRender(withKeymap(node), {
    width: options?.width ?? 80,
    height: options?.height ?? 24,
    exitOnCtrlC: false
  })
  // renderOnce → flush → renderOnce: flush awaits async work (scrollbox measure,
  // Tree-sitter markdown tokenization) that a single sync pass would miss. The
  // native `<markdown internalBlockMode="top-level">` commits blocks over several
  // native frames, so settle to visual idle too (best-effort).
  await setup.renderOnce()
  await setup.flush()
  await setup.waitForVisualIdle?.()
  await setup.renderOnce()
  await setup.flush()

  return {
    frame: () => setup.captureCharFrame(),
    waitForFrame: predicate => setup.waitForFrame(predicate),
    resize: (width, height) => setup.resize(width, height),
    destroy: () => setup.renderer.destroy?.()
  }
}

/**
 * Mount, capture one settled frame, tear down. When `until` is given (string or
 * RegExp), waits for the frame to contain/match it first — use for async
 * markdown content that may not be painted on the first settled pass.
 */
export async function captureFrame(
  node: () => JSX.Element,
  options?: { width?: number; height?: number; until?: string | RegExp }
): Promise<string> {
  const probe = await renderProbe(node, options)
  try {
    const until = options?.until
    if (until !== undefined) {
      const match = (frame: string) => (typeof until === 'string' ? frame.includes(until) : until.test(frame))
      return await probe.waitForFrame(match)
    }
    return probe.frame()
  } finally {
    probe.destroy()
  }
}
