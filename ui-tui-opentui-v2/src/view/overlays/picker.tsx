/**
 * Picker — a generic titled `<select>` overlay (spec §2b). Powers the model
 * picker (/model) and skills hub (/skills); the chosen value runs `onPick`.
 * Native select nav (↑↓/j/k/Enter); a small useKeyboard adds Esc/Ctrl+C close.
 * Replaces the composer while open.
 */
import type { BoxRenderable } from '@opentui/core'
import { createMemo } from 'solid-js'

import type { PickerItem } from '../../logic/store.ts'
import { useCloseLayer } from '../keymap.tsx'
import { useTheme } from '../theme.tsx'

export function Picker(props: {
  title: string
  items: PickerItem[]
  onPick: (value: string) => void
  onClose: () => void
}) {
  const theme = useTheme()
  let rootRef: BoxRenderable | undefined
  // Native select handles ↑↓/j/k/Enter; the keymap owns Esc/Ctrl+C close.
  useCloseLayer(
    () => rootRef,
    () => props.onClose()
  )

  const options = createMemo(() =>
    props.items.map(it => ({ description: it.description ?? '', name: it.label, value: it.value }))
  )

  return (
    <box
      ref={el => (rootRef = el)}
      style={{ borderColor: theme().color.border, flexDirection: 'column', flexShrink: 0, marginTop: 1, padding: 1 }}
      border
    >
      <text fg={theme().color.accent}>
        <b>{props.title}</b>
      </text>
      <select
        focused
        options={options()}
        onSelect={(_index, option) => {
          if (option) props.onPick(String(option.value))
        }}
        backgroundColor={theme().color.statusBg}
        selectedBackgroundColor={theme().color.selectionBg}
        textColor={theme().color.text}
        selectedTextColor={theme().color.text}
        descriptionColor={theme().color.muted}
        style={{ height: Math.min(16, Math.max(2, options().length * 2)), marginTop: 1 }}
      />
      <text fg={theme().color.muted}>↑↓ select · Enter choose · Esc cancel</text>
    </box>
  )
}
