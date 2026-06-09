/**
 * Scroll anchoring for collapse/expand toggles (item #4). The transcript
 * <scrollbox> has stickyScroll+stickyStart="bottom": on a content-height change
 * it re-pins to the bottom whenever the user hasn't manually scrolled away
 * (@opentui/core ScrollBox: `if (stickyStart && !_hasManualScroll) applyStickyStart`).
 * So expanding a tool/thinking block while at the bottom yanks the viewport to the
 * NEW bottom — scrolling the header you just clicked up off-screen.
 *
 * Fix: keep scrollTop constant across the toggle. The clicked element's document
 * position is unchanged (content grows BELOW it), so holding scrollTop keeps that
 * header at the same screen row and simply reveals the expansion beneath it. We
 * re-assert the saved offset over a few frames because the content height (and the
 * sticky re-pin) only settle on the next render pass.
 */
import { type Accessor, createContext, type JSX, useContext } from 'solid-js'

import type { ScrollBoxRenderable } from '@opentui/core'

type AnchorFn = (toggle: () => void) => void

const Ctx = createContext<AnchorFn>()

export function ScrollAnchorProvider(props: {
  scroll: Accessor<ScrollBoxRenderable | undefined>
  children: JSX.Element
}) {
  const around: AnchorFn = toggle => {
    const sb = props.scroll()
    if (!sb) {
      toggle()
      return
    }
    const prev = sb.scrollTop
    toggle()
    // Re-assert across the next few frames: the layout + sticky re-pin land on
    // subsequent render passes, so a single sync restore wouldn't hold.
    let n = 0
    const hold = () => {
      try {
        sb.scrollTo(prev)
      } catch {
        /* renderable torn down */
      }
      if (++n < 4) setTimeout(hold, 16)
    }
    setTimeout(hold, 0)
  }
  return <Ctx.Provider value={around}>{props.children}</Ctx.Provider>
}

/** Wrap a collapse/expand toggle so the viewport stays put (no-op outside a provider). */
export function useScrollAnchor(): AnchorFn {
  return useContext(Ctx) ?? (toggle => toggle())
}
