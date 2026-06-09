/**
 * ConfirmPrompt — a LOCAL (non-gateway) Y/N dialog (spec §2a). Driven by a local
 * callback, not an RPC: y/Enter → confirm, n/Esc/Ctrl+C → cancel. Used by client
 * slash commands like /clear and /new.
 */
import type { BoxRenderable } from '@opentui/core'
import { useBindings } from '@opentui/keymap/solid'
import { onMount } from 'solid-js'

import { useTheme } from '../theme.tsx'

export function ConfirmPrompt(props: { message: string; onYes: () => void; onNo: () => void }) {
  const theme = useTheme()
  let rootRef: BoxRenderable | undefined
  // No focusable child here (unlike the <select> prompts), so focus the dialog box
  // itself on mount — that makes the focus-within keymap layer below active.
  onMount(() => rootRef?.focus())
  // Local Y/N dialog: y/Enter → confirm, n/Esc/Ctrl+C → cancel, scoped to the
  // dialog box (focus-within) via the native keymap.
  useBindings<BoxRenderable>(() => ({
    target: () => rootRef,
    commands: [
      {
        name: 'confirm',
        run() {
          props.onYes()
        }
      },
      {
        name: 'cancel',
        run() {
          props.onNo()
        }
      }
    ],
    bindings: [
      { key: 'y', cmd: 'confirm' },
      { key: 'return', cmd: 'confirm' },
      { key: 'n', cmd: 'cancel' },
      { key: 'escape', cmd: 'cancel' },
      { key: { name: 'c', ctrl: true }, cmd: 'cancel' }
    ]
  }))

  return (
    <box
      ref={el => (rootRef = el)}
      focusable
      style={{ borderColor: theme().color.border, flexDirection: 'column', flexShrink: 0, marginTop: 1, padding: 1 }}
      border
    >
      <text fg={theme().color.warn}>
        <b>{props.message}</b>
      </text>
      <text fg={theme().color.muted}>y/Enter confirm · n/Esc cancel</text>
    </box>
  )
}
