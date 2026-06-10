/**
 * BashTool — renderer for the shell-ish tools `terminal`, `execute_code` and
 * `process` (Epic 2.4). Collapsed: the COMMAND BEING INVOKED, verbatim, on one
 * line (the shell truncates to width; expanding reveals the rest). Expanded:
 * the full command (`$ `-prefixed, multi-line safe) and the FULL output, kept
 * to the EXPANDED_MAX cap with the honest omitted / "+N more lines" notes from
 * `logic/toolOutput.ts` (via the shared ToolOutputBlock).
 *
 * Arg keys verified against the Python tool schemas:
 *   terminal     → `command`                 (tools/terminal_tool.py TERMINAL_SCHEMA)
 *   execute_code → `code`                    (tools/code_execution_tool.py)
 *   process      → `action` (+ `session_id`) (tools/process_registry.py PROCESS_SCHEMA)
 * Falls back to the gateway's one-line argsPreview when args weren't captured.
 */
import { createMemo, For, Show } from 'solid-js'

import type { ToolPartState } from '../../logic/store.ts'
import { truncate } from '../../logic/toolOutput.ts'
import { useTheme } from '../theme.tsx'
import { defaultSubtitle, resultLines, structuredArgs, ToolOutputBlock } from './defaultTool.tsx'
import type { ToolBodyProps, ToolRenderer } from './registry.tsx'

/** The verbatim invocation: terminal `command` / execute_code `code` /
 *  process `action [session_id]`; else the gateway's argsPreview. Via
 *  structuredArgs this prefers the gateway-redacted args_text parse — a
 *  masked command string is the CORRECT display when the gateway redacted it. */
export function commandOf(part: ToolPartState): string {
  const args = structuredArgs(part)
  if (args) {
    const cmd = args['command'] ?? args['code']
    if (typeof cmd === 'string' && cmd.trim()) return cmd
    const action = args['action'] // process: the verb is the invocation
    if (typeof action === 'string' && action) {
      const sid = args['session_id']
      const sidText = typeof sid === 'string' || typeof sid === 'number' ? String(sid) : ''
      return sidText ? `${action} ${sidText}` : action
    }
  }
  return part.argsPreview ?? ''
}

/** Expanded body: the full `$ command`, then the full (capped) output. */
export function BashToolBody(props: ToolBodyProps) {
  const theme = useTheme()
  const command = createMemo(() => commandOf(props.part).replace(/\s+$/, ''))
  return (
    <box style={{ flexDirection: 'column', flexGrow: 1, minWidth: 0 }}>
      <Show when={command()}>
        <For each={command().split('\n')}>
          {(line, i) => (
            <box style={{ flexDirection: 'row', flexShrink: 0 }}>
              {/* `$ ` prompt glyph (continuation lines indent under it) — chrome */}
              <text selectable={false}>
                <span style={{ fg: theme().color.accent }}>{i() === 0 ? '$ ' : '  '}</span>
              </text>
              {/* the command itself is copyable content */}
              <text selectionBg={theme().color.selectionBg}>
                <span style={{ fg: theme().color.text }}>{truncate(line, Math.max(1, props.width - 2))}</span>
              </text>
            </box>
          )}
        </For>
      </Show>
      <ToolOutputBlock part={props.part} width={props.width} label={Boolean(command())} />
    </box>
  )
}

export const bashRenderer: ToolRenderer = {
  Body: BashToolBody,
  // Collapsed never shows output (the header shows the command), so ANY output
  // is hidden content worth expanding — as is a multi-line command.
  expandable: part => resultLines(part).length > 0 || commandOf(part).includes('\n'),
  // The command, verbatim, flattened to one line (the shell truncates to width).
  subtitle: part => commandOf(part).replace(/\s+/g, ' ').trim() || defaultSubtitle(part)
}
