/**
 * Tool renderer tests (Epics 2.2 + 2.4). Headless frames through the real App
 * tree: the registry's default renderer turns args into LABELED FIELDS — the
 * acceptance gate asserts NO raw JSON syntax (`{"` / `":`) ever reaches the
 * frame for tool parts, collapsed or expanded — delegate_task carries the
 * Ink-parity "(/agents to monitor)" hint, and the bash renderer shows the
 * command verbatim collapsed + the full (EXPANDED_MAX-capped) output expanded.
 * Expansion goes through the REAL mouse path: mockMouse clicks the header row
 * (found by scanning the frame). The long-output cap is asserted at the Body
 * level (a tall frame would otherwise hide the trailing note).
 */
import { describe, expect, test } from 'vitest'

import { createSessionStore, type ToolPartState } from '../logic/store.ts'
import { App } from '../view/App.tsx'
import { ThemeProvider } from '../view/theme.tsx'
import { BashToolBody, commandOf } from '../view/tools/bashTool.tsx'
import { renderProbe, type RenderProbe } from './lib/render.ts'

type Store = ReturnType<typeof createSessionStore>

/** Seed a settled assistant turn containing exactly the given tool call. */
function seedTool(store: Store, start: Record<string, unknown>, complete: Record<string, unknown>) {
  store.apply({ type: 'gateway.ready' })
  store.apply({ type: 'message.start' })
  store.apply({ type: 'tool.start', payload: start })
  store.apply({ type: 'tool.complete', payload: complete })
  store.apply({ type: 'message.complete' })
}

async function mountApp(store: Store, width = 80, height = 24): Promise<RenderProbe> {
  return renderProbe(
    () => (
      <ThemeProvider theme={() => store.state.theme}>
        <App store={store} />
      </ThemeProvider>
    ),
    { width, height }
  )
}

/** Click the tool header row (the line containing `name`) to expand/collapse. */
async function clickHeader(probe: RenderProbe, name: string): Promise<void> {
  const frame = await probe.waitForFrame(f => f.includes(name))
  const rows = frame.split('\n')
  const y = rows.findIndex(line => line.includes(name))
  expect(y).toBeGreaterThanOrEqual(0)
  const x = (rows[y] ?? '').indexOf(name)
  await probe.click(x, y)
}

describe('tool renderer registry — labeled-args default (Epic 2.2)', () => {
  test('an unmapped MCP-ish tool with nested args renders labeled fields, never raw JSON', async () => {
    const store = createSessionStore()
    seedTool(
      store,
      { tool_id: 'm1', name: 'mcp_lookup' },
      {
        tool_id: 'm1',
        name: 'mcp_lookup',
        args: {
          query: 'hermes agent',
          options: { depth: 2, mode: 'fast', cache: true },
          limit: 5
        },
        duration_s: 0.4,
        result_text: 'one result found'
      }
    )

    const probe = await mountApp(store)
    try {
      // collapsed: header only, and already no JSON syntax anywhere
      const collapsed = await probe.waitForFrame(f => f.includes('mcp_lookup'))
      expect(collapsed).not.toContain('{"')
      expect(collapsed).not.toContain('":')

      await clickHeader(probe, 'mcp_lookup')
      const expanded = await probe.waitForFrame(f => f.includes('query'))
      // labeled key → value rows (string verbatim, number via String)
      expect(expanded).toContain('query')
      expect(expanded).toContain('hermes agent')
      expect(expanded).toContain('limit')
      expect(expanded).toContain('5')
      // nested object summarized, not dumped
      expect(expanded).toContain('options')
      expect(expanded).toContain('(3 fields)')
      // the output body still renders (envelope-stripped store text)
      expect(expanded).toContain('one result found')
      // THE acceptance gate: no raw JSON syntax in the tool render
      expect(expanded).not.toContain('{"')
      expect(expanded).not.toContain('":')
      expect(expanded).not.toContain('depth') // nested internals stay summarized
    } finally {
      probe.destroy()
    }
  })

  test('delegate_task gets the default renderer plus the muted "(/agents to monitor)" hint', async () => {
    const store = createSessionStore()
    seedTool(
      store,
      { tool_id: 'd1', name: 'delegate_task', context: 'research opentui' },
      {
        tool_id: 'd1',
        name: 'delegate_task',
        args: { goal: 'research opentui', model: 'fast' },
        result_text: 'spawned'
      }
    )

    const probe = await mountApp(store)
    try {
      const frame = await probe.waitForFrame(f => f.includes('(/agents to monitor)'))
      expect(frame).toContain('delegate_task')
      expect(frame).toContain('research opentui') // primary-arg preview still leads
      expect(frame).not.toContain('{"') // hint or not — still no raw JSON
    } finally {
      probe.destroy()
    }
  })
})

describe('bash tool renderer — command + full output (Epic 2.4)', () => {
  test('collapsed header shows the invoked command VERBATIM (args win over the gateway preview)', async () => {
    const store = createSessionStore()
    seedTool(
      store,
      // the gateway's one-line preview is truncated — args.command is the truth
      { tool_id: 'b1', name: 'terminal', context: 'grep -rn needle' },
      {
        tool_id: 'b1',
        name: 'terminal',
        args: { command: 'grep -rn needle src/ | head -5', timeout: 60 },
        duration_s: 0.2,
        result_text: 'a.ts:1:needle\nb.ts:2:needle\nc.ts:3:needle'
      }
    )

    const probe = await mountApp(store)
    try {
      const frame = await probe.waitForFrame(f => f.includes('grep -rn needle src/ | head -5'))
      expect(frame).toContain('terminal')
      expect(frame).toContain('grep -rn needle src/ | head -5') // verbatim, not the preview
      expect(frame).toContain('(3 lines)') // output stays behind the expand affordance
      expect(frame).not.toContain('a.ts:1:needle') // collapsed → no output shown
    } finally {
      probe.destroy()
    }
  })

  test('expanded shows the $ command and the FULL (short) output', async () => {
    const store = createSessionStore()
    seedTool(
      store,
      { tool_id: 'b2', name: 'terminal' },
      {
        tool_id: 'b2',
        name: 'terminal',
        args: { command: 'ls' },
        result_text: 'alpha.txt\nbeta.txt\ngamma.txt'
      }
    )

    const probe = await mountApp(store)
    try {
      await clickHeader(probe, 'terminal')
      const expanded = await probe.waitForFrame(f => f.includes('alpha.txt'))
      expect(expanded).toContain('$ ls') // the invocation, prompt-prefixed
      expect(expanded).toContain('output') // section label
      expect(expanded).toContain('alpha.txt') // full output…
      expect(expanded).toContain('beta.txt')
      expect(expanded).toContain('gamma.txt') // …down to the last line
    } finally {
      probe.destroy()
    }
  })

  test('long output is capped to EXPANDED_MAX with an honest "+N more lines" note', async () => {
    const lines = Array.from({ length: 250 }, (_, i) => `line-${String(i + 1).padStart(3, '0')}`)
    const part: ToolPartState = {
      type: 'tool',
      id: 'b3',
      name: 'execute_code',
      state: 'complete',
      args: { code: 'for i in range(250): print(i)' },
      resultText: lines.join('\n')
    }
    // Body-level mount (tall frame so the trailing note row is on screen).
    const probe = await renderProbe(
      () => (
        <ThemeProvider>
          <BashToolBody part={part} width={70} />
        </ThemeProvider>
      ),
      { width: 80, height: 210 }
    )
    try {
      const frame = await probe.waitForFrame(f => f.includes('+50 more lines'))
      expect(frame).toContain('$ for i in range(250): print(i)')
      expect(frame).toContain('line-001') // the cap keeps the HEAD of the output
      expect(frame).toContain('line-200') // …up to EXPANDED_MAX
      expect(frame).not.toContain('line-201') // the rest is honestly elided
      expect(frame).toContain('… +50 more lines')
    } finally {
      probe.destroy()
    }
  })

  test('a gateway-capped result renders the tidy omitted note', async () => {
    const part: ToolPartState = {
      type: 'tool',
      id: 'b4',
      name: 'terminal',
      state: 'complete',
      args: { command: 'cat big.log' },
      resultText: 'tail line one\ntail line two',
      omittedNote: '120 lines / 9001 chars'
    }
    const probe = await renderProbe(
      () => (
        <ThemeProvider>
          <BashToolBody part={part} width={70} />
        </ThemeProvider>
      ),
      { width: 80, height: 12 }
    )
    try {
      const frame = await probe.waitForFrame(f => f.includes('omitted'))
      expect(frame).toContain('tail line one')
      expect(frame).toContain('… omitted 120 lines / 9001 chars')
    } finally {
      probe.destroy()
    }
  })
})

describe('redaction precedence — gateway args_text wins over raw args (security)', () => {
  // The gateway redacts verbose `args_text` (server.py _tool_args_text) but
  // sends the raw `args` dict on tool.complete UNREDACTED. structuredArgs must
  // parse argsText first so masked secrets never render unmasked.

  test('labeled fields render the redacted args_text value, never the raw args secret', async () => {
    const store = createSessionStore()
    seedTool(
      store,
      // verbose session: tool.start carries the gateway-redacted args_text
      {
        tool_id: 's1',
        name: 'mcp_call',
        args_text: JSON.stringify({ api_key: 'sk-****', endpoint: 'v1/users' }, null, 2)
      },
      // tool.complete carries the raw, UNREDACTED args dict
      {
        tool_id: 's1',
        name: 'mcp_call',
        args: { api_key: 'sk-secret123', endpoint: 'v1/users' },
        result_text: 'done'
      }
    )

    const probe = await mountApp(store)
    try {
      await clickHeader(probe, 'mcp_call')
      const expanded = await probe.waitForFrame(f => f.includes('api_key'))
      expect(expanded).toContain('sk-****') // the gateway's redaction survives
      expect(expanded).not.toContain('sk-secret123') // the raw secret never renders
      expect(expanded).toContain('endpoint') // non-secret fields still labeled
      expect(expanded).toContain('v1/users')
    } finally {
      probe.destroy()
    }
  })

  test('commandOf prefers the redacted args_text parse over the raw args command', () => {
    const store = createSessionStore()
    seedTool(
      store,
      {
        tool_id: 's2',
        name: 'terminal',
        args_text: JSON.stringify({ command: 'curl -H "Authorization: sk-****" api.test' })
      },
      {
        tool_id: 's2',
        name: 'terminal',
        args: { command: 'curl -H "Authorization: sk-secret123" api.test' },
        result_text: 'ok'
      }
    )
    // Going through the real store also pins the invariant this fix relies on:
    // tool.complete back-fills argsText only when ABSENT — the redacted
    // tool.start args_text is never overwritten.
    const last = store.state.messages[store.state.messages.length - 1]
    const part = last?.parts?.find((p): p is ToolPartState => p.type === 'tool' && p.id === 's2')
    expect(part).toBeDefined()
    expect(part?.argsText).toContain('sk-****')
    const cmd = commandOf(part as ToolPartState)
    expect(cmd).toContain('sk-****') // a masked command IS the correct display
    expect(cmd).not.toContain('sk-secret123')
  })

  test('absent or unparseable argsText falls back to raw args (non-verbose parity)', () => {
    // no argsText at all → raw args, same as the previous behavior
    const bare: ToolPartState = {
      type: 'tool',
      id: 's3',
      name: 'terminal',
      state: 'complete',
      args: { command: 'ls -la' }
    }
    expect(commandOf(bare)).toBe('ls -la')
    // argsText capped mid-JSON (unparseable) → raw args still render
    const capped: ToolPartState = {
      type: 'tool',
      id: 's4',
      name: 'terminal',
      state: 'complete',
      args: { command: 'echo hi' },
      argsText: '{"command": "echo h'
    }
    expect(commandOf(capped)).toBe('echo hi')
  })
})
