/**
 * ReasoningPart — the model's thinking trace, collapsible (item 6; opencode's
 * ReasoningPart/ReasoningHeader). Auto-EXPANDED while the turn streams (so you
 * watch it think), then COLLAPSES to a one-line `▶ Thought: <title>` once the
 * turn settles. Click the header to override either way.
 *
 *   ▼ Thinking: <title>        ← live (streaming), body shown
 *   ▶ Thought: <title>         ← settled (collapsed), click to reopen
 *   │ <reasoning markdown>     ← dim body in a left-bordered block
 *
 * Title is the model's leading `**bold**` line when present (opencode's
 * reasoningSummary). Dim throughout — it's secondary to the answer.
 */
import { createMemo, createSignal, Show } from 'solid-js'

import { Markdown } from './markdown.tsx'
import { useScrollAnchor } from './scrollAnchor.tsx'
import { useTheme } from './theme.tsx'

const GUTTER = 2

/** Split a leading `**Title**\n\n body` into {title, body} (opencode reasoningSummary). */
function reasoningSummary(text: string): { title?: string; body: string } {
  const s = (text ?? '').replace('[REDACTED]', '').trim()
  const m = s.match(/^\*\*([^*\n]+)\*\*(?:\r?\n\r?\n|$)/)
  const title = m?.[1]?.trim()
  if (!m || !title) return { body: s }
  return { title, body: s.slice(m[0].length).trimStart() }
}

export function ReasoningPart(props: { text: string; streaming?: boolean }) {
  const theme = useTheme()
  const anchor = useScrollAnchor()
  const [override, setOverride] = createSignal<boolean | undefined>(undefined)
  // live → expanded so you see it think; settled → collapsed. Click overrides.
  const expanded = () => override() ?? !!props.streaming
  const toggle = () => anchor(() => setOverride(e => !(e ?? !!props.streaming)))
  const summary = createMemo(() => reasoningSummary(props.text))
  const label = () => (props.streaming ? 'Thinking' : 'Thought')

  return (
    <Show when={summary().body || summary().title}>
      <box style={{ flexDirection: 'column', flexShrink: 0 }}>
        <box style={{ flexDirection: 'row', flexShrink: 0 }} onMouseDown={toggle}>
          <box style={{ flexShrink: 0, width: GUTTER }}>
            <text selectable={false}>
              <span style={{ fg: theme().color.accent }}>{expanded() ? '▼' : '▶'}</span>
            </text>
          </box>
          {/* the header is a collapsible-section LABEL (Thinking/Thought + title)
              — chrome, not the reasoning body — so a free-form drag yields only
              the markdown body below, not the section label (item 4). */}
          <text selectable={false}>
            {/* accent chevron marks it; muted label keeps reasoning in the dim,
                secondary tier alongside tool calls (Ink hierarchy). */}
            <span style={{ fg: theme().color.muted }}>{label()}</span>
            <Show when={summary().title}>
              <span style={{ fg: theme().color.muted }}>{`: ${summary().title}`}</span>
            </Show>
          </text>
        </box>
        <Show when={expanded() && summary().body}>
          <box
            style={{ flexDirection: 'column', flexGrow: 1, minWidth: 0, marginLeft: GUTTER, paddingLeft: 1 }}
            border={['left']}
            borderColor={theme().color.border}
          >
            <Markdown text={summary().body} streaming={props.streaming ?? false} fg={theme().color.muted} />
          </box>
        </Show>
      </box>
    </Show>
  )
}
