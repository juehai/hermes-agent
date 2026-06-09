/**
 * HomeHint — the empty-transcript home screen (items 12 + 9; Ink `branding.tsx`
 * parity). The HERMES-AGENT banner + a tagline, then a session info block
 * (model · Nous Research / dir / Session id), then SEPARATE collapsible sections —
 * Available Tools (enabled toolsets + their tools), Available Skills, MCP Servers —
 * and a summary line. Fully themed; decorative, so `selectable={false}` (item 4).
 */
import { createSignal, For, type JSX, Show } from 'solid-js'

import type { SessionStore } from '../logic/store.ts'
import { truncate } from '../logic/toolOutput.ts'
import { useDimensions } from './dimensions.tsx'
import { useTheme } from './theme.tsx'

// The canonical HERMES-AGENT block logo (hermes_cli/banner.py), gold→amber→bronze.
const BANNER: ReadonlyArray<readonly [string, 'primary' | 'accent' | 'border']> = [
  ['██╗  ██╗███████╗██████╗ ███╗   ███╗███████╗███████╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗', 'primary'],
  ['██║  ██║██╔════╝██╔══██╗████╗ ████║██╔════╝██╔════╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝', 'primary'],
  ['███████║█████╗  ██████╔╝██╔████╔██║█████╗  ███████╗█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║', 'accent'],
  ['██╔══██║██╔══╝  ██╔══██╗██║╚██╔╝██║██╔══╝  ╚════██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║', 'accent'],
  ['██║  ██║███████╗██║  ██║██║ ╚═╝ ██║███████╗███████║      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║', 'border'],
  ['╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝      ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝', 'border']
]
const BANNER_W = 102
const TOOLSETS_MAX = 10

/** `anthropic/claude-opus-4-8` → `claude-opus-4-8`. */
const shortModel = (m: string) => (m.includes('/') ? (m.split('/').at(-1) ?? m) : m)
const HOME = process.env.HOME ?? ''
const shortCwd = (cwd: string) => (HOME && cwd.startsWith(HOME) ? '~' + cwd.slice(HOME.length) : cwd)

export function HomeHint(props: { store: SessionStore }) {
  const theme = useTheme()
  const dims = useDimensions()
  const wide = () => dims().width >= BANNER_W
  const cat = () => props.store.state.catalog
  const info = () => props.store.state.info
  const enabledToolsets = () => (cat()?.tools.toolsets ?? []).filter(t => t.enabled)

  // A collapsible section: ▸/▾ accent chevron + label title + optional muted suffix.
  function Section(p: { title: string; suffix?: string; open?: boolean; children: JSX.Element }) {
    const [open, setOpen] = createSignal(p.open ?? false)
    return (
      <box style={{ flexDirection: 'column', marginTop: 1 }}>
        <box style={{ flexDirection: 'row', flexShrink: 0 }} onMouseDown={() => setOpen(o => !o)}>
          <text selectable={false}>
            <span style={{ fg: theme().color.accent }}>{open() ? '▾ ' : '▸ '}</span>
            <span style={{ fg: theme().color.label }}>{p.title}</span>
            <Show when={p.suffix}>
              <span style={{ fg: theme().color.muted }}>{` ${p.suffix}`}</span>
            </Show>
          </text>
        </box>
        <Show when={open()}>
          <box
            style={{ flexDirection: 'column', marginLeft: 2, paddingLeft: 1 }}
            border={['left']}
            borderColor={theme().color.border}
          >
            {p.children}
          </box>
        </Show>
      </box>
    )
  }

  return (
    <box style={{ flexDirection: 'column', flexShrink: 0, paddingLeft: 1, marginTop: 1 }}>
      {/* banner — full block logo when there's room, else a compact brand line */}
      <Show
        when={wide()}
        fallback={
          <text selectable={false}>
            <span style={{ fg: theme().color.accent }}>{theme().brand.icon} </span>
            <span style={{ fg: theme().color.primary }}>
              <b>{theme().brand.name}</b>
            </span>
          </text>
        }
      >
        <For each={BANNER}>
          {([line, tone]) => (
            <text selectable={false}>
              <span style={{ fg: theme().color[tone] }}>{line}</span>
            </text>
          )}
        </For>
      </Show>
      <text selectable={false}>
        <span style={{ fg: theme().color.accent }}>{`${theme().brand.icon} `}</span>
        <span style={{ fg: theme().color.muted }}>Nous Research · Messenger of the Digital Gods</span>
      </text>

      {/* framed session panel (Ink SessionPanel parity) — the bordered box is the
          key "this is a designed home screen, not log output" signal. */}
      <box
        style={{ flexDirection: 'column', marginTop: 1, paddingLeft: 1, paddingRight: 1 }}
        border
        borderColor={theme().color.border}
      >
        {/* session info block: model · Nous Research / dir / Session id */}
        <box style={{ flexDirection: 'column' }}>
          <Show when={info().model}>
            {model => (
              <text selectable={false}>
                <span style={{ fg: theme().color.accent }}>{shortModel(model())}</span>
                <span style={{ fg: theme().color.muted }}> · Nous Research</span>
              </text>
            )}
          </Show>
          <Show when={info().cwd}>
            {cwd => (
              <text selectable={false}>
                <span style={{ fg: theme().color.muted }}>{shortCwd(cwd())}</span>
                <Show when={info().branch}>
                  <span style={{ fg: theme().color.muted }}>{` (${info().branch})`}</span>
                </Show>
              </text>
            )}
          </Show>
          <Show when={props.store.state.sessionId}>
            <text selectable={false}>
              <span style={{ fg: theme().color.muted }}>Session: </span>
              <span style={{ fg: theme().color.border }}>{props.store.state.sessionId}</span>
            </text>
          </Show>
        </box>

        {/* SEPARATE collapsible sections (Ink parity) + summary */}
        <Show when={cat()}>
          {c => (
            <box style={{ flexDirection: 'column' }}>
              <Section title="Available Tools" open>
                <For each={enabledToolsets().slice(0, TOOLSETS_MAX)}>
                  {ts => (
                    <text selectable={false}>
                      <span style={{ fg: theme().color.label }}>{`${ts.name}: `}</span>
                      <span style={{ fg: theme().color.muted }}>
                        {truncate(
                          ts.tools.join(', ') || `${ts.count} tools`,
                          Math.max(20, dims().width - ts.name.length - 8)
                        )}
                      </span>
                    </text>
                  )}
                </For>
                <Show when={enabledToolsets().length > TOOLSETS_MAX}>
                  <text selectable={false}>
                    <span
                      style={{ fg: theme().color.muted }}
                    >{`(and ${enabledToolsets().length - TOOLSETS_MAX} more toolsets…)`}</span>
                  </text>
                </Show>
              </Section>

              <Section
                title={`Available Skills (${c().skills.total})`}
                suffix={`in ${c().skills.categories.length} categories`}
              >
                <text selectable={false}>
                  <span style={{ fg: theme().color.muted }}>
                    {c()
                      .skills.categories.map(s => `${s.name} (${s.count})`)
                      .join('  ')}
                  </span>
                </text>
              </Section>

              <Section
                title={`MCP Servers (${c().mcp.servers.length})`}
                suffix={c().mcp.servers.length ? 'connected' : ''}
              >
                <text selectable={false}>
                  <span style={{ fg: theme().color.muted }}>{c().mcp.servers.join('  ') || 'none configured'}</span>
                </text>
              </Section>

              <box style={{ marginTop: 1 }}>
                <text selectable={false}>
                  <span style={{ fg: theme().color.text }}>{`${c().tools.total} tools`}</span>
                  <span
                    style={{ fg: theme().color.muted }}
                  >{` · ${c().skills.total} skills · ${c().mcp.servers.length} MCP · `}</span>
                  <span style={{ fg: theme().color.accent }}>/help</span>
                  <span style={{ fg: theme().color.muted }}> for commands</span>
                </text>
              </box>
            </box>
          )}
        </Show>
      </box>
      {/* end framed session panel */}

      <box style={{ marginTop: 1 }}>
        <text selectable={false}>
          <span style={{ fg: theme().color.muted }}>
            Type to chat · ↑↓ history · @file to mention · Ctrl+C to stop/quit
          </span>
        </text>
      </box>
    </box>
  )
}
