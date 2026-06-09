/**
 * keymap.tsx — thin Solid helpers over the native `@opentui/keymap` (Phase 3).
 *
 * `useCloseLayer` is the shared CLOSE binding for overlays/prompts: a `close`
 * command bound to Esc and Ctrl+C, scoped to the overlay's root box via a
 * `focus-within` layer (the default when a `target` accessor is present). The
 * box itself isn't focused — the native `<select>`/`<textarea>` inside it is —
 * so `focus-within` is what makes the layer active while the overlay owns the
 * screen. The keymap host is provided once at the entry by `<KeymapProvider>`.
 */
import type { BoxRenderable } from '@opentui/core'
import { useBindings } from '@opentui/keymap/solid'

/**
 * Bind Esc / Ctrl+C → `onClose`, scoped to the given root box (focus-within).
 * Until the ref resolves the layer simply isn't registered (useBindings waits).
 */
export function useCloseLayer(target: () => BoxRenderable | undefined, onClose: () => void): void {
  useBindings<BoxRenderable>(() => ({
    target,
    commands: [
      {
        name: 'close',
        run() {
          onClose()
        }
      }
    ],
    bindings: [
      { key: 'escape', cmd: 'close' },
      { key: { name: 'c', ctrl: true }, cmd: 'close' }
    ]
  }))
}
