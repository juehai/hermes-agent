/**
 * toolOutput unit test (spec v4 §5 Layer 4 — Hermes-specific contract). The
 * `{output,exit_code}` envelope unwrap + the line/char collapse, as pure data.
 */
import { describe, expect, test } from 'bun:test'

import { collapseToolOutput, stripAnsi, stripOmittedNote, stripToolEnvelope, truncate } from '../logic/toolOutput.ts'

describe('stripAnsi (item 8 - gateway slash/notice text is ANSI-colored for Ink)', () => {
  const ESC = String.fromCharCode(27)
  test('removes SGR color codes, keeps the text', () => {
    expect(stripAnsi(`${ESC}[1;38;2;255;215;0m\u2713 Reasoning display: ON${ESC}[0m`)).toBe(
      '\u2713 Reasoning display: ON'
    )
  })
  test('removes italic + mouse sequences', () => {
    expect(stripAnsi(`${ESC}[2;3m  Model thinking shown.${ESC}[0m`)).toBe('  Model thinking shown.')
    expect(stripAnsi(`hi${ESC}[<0;6;8mthere`)).toBe('hithere')
  })
  test('leaves plain text untouched', () => {
    expect(stripAnsi('just text')).toBe('just text')
  })
})

describe('stripOmittedNote (item 2 — peel the gateway verbose-tail label)', () => {
  test('extracts the lines/chars note and returns the clean body', () => {
    const { body, omittedNote } = stripOmittedNote(
      '[showing verbose tail; omitted 5 lines / 234 chars]\nline one\nline two'
    )
    expect(omittedNote).toBe('5 lines / 234 chars')
    expect(body).toBe('line one\nline two')
  })
  test('extracts a chars-only note', () => {
    const { body, omittedNote } = stripOmittedNote('[showing verbose tail; omitted 512 chars]\ntail body')
    expect(omittedNote).toBe('512 chars')
    expect(body).toBe('tail body')
  })
  test('passes through unlabeled output untouched', () => {
    const { body, omittedNote } = stripOmittedNote('normal output\nno prefix')
    expect(omittedNote).toBeUndefined()
    expect(body).toBe('normal output\nno prefix')
  })
})

describe('stripToolEnvelope', () => {
  test('unwraps {output,exit_code} → output', () => {
    expect(stripToolEnvelope('{"output":"hi","exit_code":0}')).toBe('hi')
  })
  test('appends an [exit N] suffix on non-zero exit', () => {
    expect(stripToolEnvelope('{"output":"oops","exit_code":2}')).toBe('oops\n[exit 2]')
  })
  test('appends an [error] suffix when error is set', () => {
    expect(stripToolEnvelope('{"output":"x","error":"boom"}')).toBe('x\n[error] boom')
  })
  test('passes through non-JSON / non-envelope unchanged', () => {
    expect(stripToolEnvelope('just text')).toBe('just text')
    expect(stripToolEnvelope('{not json')).toBe('{not json')
    expect(stripToolEnvelope('{"result":"no output key"}')).toBe('{"result":"no output key"}')
  })
  test('unwraps a TAIL-capped envelope fragment (item 2 — gateway serialises then tail-caps)', () => {
    // head was cut, tail keeps the envelope close → strip the trailing close
    expect(stripToolEnvelope('zsh\nzutty", "exit_code": 0, "error": null}')).toBe('zsh\nzutty')
    // head survived, tail cut → strip the leading {"output": "
    expect(stripToolEnvelope('{"output": "line1\nline2')).toBe('line1\nline2')
    // real output that merely mentions exit_code is NOT mangled
    expect(stripToolEnvelope('the exit_code was 0 here')).toBe('the exit_code was 0 here')
  })
  test('un-double-escapes literal \\n when they dominate (item 7 verbose tail)', () => {
    // double-escaped output (literal backslash-n) → real newlines
    expect(stripToolEnvelope('a\\nb\\nc')).toBe('a\nb\nc')
    // genuine multi-line output (real newlines) with one literal \n is left alone
    expect(stripToolEnvelope('line1\nline2\nshow \\n here')).toBe('line1\nline2\nshow \\n here')
  })
})

describe('collapseToolOutput / truncate', () => {
  test('caps to maxLines and reports the hidden count', () => {
    const c = collapseToolOutput('a\nb\nc\nd', 2, 10)
    expect(c.lines).toEqual(['a', 'b'])
    expect(c.hiddenLines).toBe(2)
    expect(c.truncated).toBe(true)
  })
  test('no truncation when within the cap', () => {
    const c = collapseToolOutput('a\nb', 5, 10)
    expect(c.lines).toEqual(['a', 'b'])
    expect(c.hiddenLines).toBe(0)
    expect(c.truncated).toBe(false)
  })
  test('truncate adds an ellipsis only when cut', () => {
    expect(truncate('abcdef', 4)).toBe('abc…')
    expect(truncate('ab', 4)).toBe('ab')
  })
})
