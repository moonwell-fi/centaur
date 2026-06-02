import { readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { mkdirSync } from 'node:fs'
import {
  codexAppServerToChatSdkStream,
  type CodexAppServerToChatStreamOptions
} from '../packages/rendering/src/codex-app-server'
import type { ChatSDKOutput, ChatSDKStreamChunk } from '../packages/rendering/src/chat-sdk'
import type { RendererEvent } from '../packages/rendering/src/types'

type SyntheticEvent = {
  id: number
  event: string
  data: string
}

type SyntheticCase = {
  case_id: string
  issue: string
  events: SyntheticEvent[]
  expected_regression_assertion: string
}

type SyntheticFile = {
  schema: string
  cases: SyntheticCase[]
}

const inputPath = process.argv[2] ?? 'local-corpus/slackbot-fuzz/synthetic-rendering-cases.json'
const outputPath =
  process.argv[3] ?? 'local-corpus/slackbot-fuzz/synthetic-rendering-observed.json'

const input = JSON.parse(readFileSync(inputPath, 'utf8')) as SyntheticFile
const observed = []

for (const testCase of input.cases) {
  const consumed = slackbotConsumedEvents(testCase.events)
  const rendered = await render(consumed.map(toRustSessionSource))
  observed.push({
    case_id: testCase.case_id,
    issue: testCase.issue,
    expected_regression_assertion: testCase.expected_regression_assertion,
    input_event_count: testCase.events.length,
    slackbot_consumed_event_count: consumed.length,
    slackbot_stopped_before_all_events: consumed.length < testCase.events.length,
    consumed_event_ids: consumed.map(event => event.id),
    markdown_text: rendered.chunks
      .filter((chunk): chunk is Extract<ChatSDKStreamChunk, { type: 'markdown_text' }> => {
        return chunk.type === 'markdown_text'
      })
      .map(chunk => chunk.text)
      .join(''),
    task_errors: rendered.chunks.filter(chunk => chunk.type === 'task_update' && chunk.status === 'error')
      .length,
    task_updates: rendered.chunks.filter(chunk => chunk.type === 'task_update'),
    plan_updates: rendered.chunks.filter(chunk => chunk.type === 'plan_update'),
    closed_messages: rendered.outputs.filter(output => output.type === 'chat.session.closed'),
    renderer_events: rendered.rendererEvents,
    observed_issues: observedIssues(testCase, consumed, rendered)
  })
}

const output = {
  schema: 'centaur.slackbot_synthetic_rendering_observed.v1',
  input: inputPath,
  case_count: observed.length,
  observed
}

mkdirSync(dirname(outputPath), { recursive: true })
writeFileSync(outputPath, `${JSON.stringify(output, null, 2)}\n`)
console.log(outputPath)

async function render(sources: unknown[]): Promise<{
  chunks: ChatSDKStreamChunk[]
  outputs: ChatSDKOutput[]
  rendererEvents: RendererEvent[]
}> {
  const chunks: ChatSDKStreamChunk[] = []
  const outputs: ChatSDKOutput[] = []
  const rendererEvents: RendererEvent[] = []
  const options: CodexAppServerToChatStreamOptions = {
    onOutput(output) {
      outputs.push(output)
    },
    onRendererEvent(event) {
      rendererEvents.push(event)
    }
  }
  for await (const chunk of codexAppServerToChatSdkStream(toAsyncIterable(sources), options)) {
    chunks.push(chunk)
  }
  return { chunks, outputs, rendererEvents }
}

function slackbotConsumedEvents(events: SyntheticEvent[]): SyntheticEvent[] {
  const consumed = []
  let sawFinalAnswerText = false
  for (const event of events) {
    if (event.event === 'session.output.line') {
      sawFinalAnswerText ||= outputLineCarriesFinalAnswerText(event.data)
      consumed.push(event)
      if (isTerminalCodexOutputLine(event.data, { sawFinalAnswerText })) return consumed
      continue
    }
    if (event.event === 'session.execution_failed' || event.event === 'session.stream_error') {
      consumed.push(event)
      return consumed
    }
    consumed.push(event)
  }
  return consumed
}

function toRustSessionSource(event: SyntheticEvent): unknown {
  if (event.event === 'session.execution_failed' || event.event === 'session.stream_error') {
    return {
      event: event.event,
      eventKind: event.event,
      eventId: event.id,
      data: { error: sessionErrorMessage(event) }
    }
  }
  return {
    event: event.event,
    eventKind: event.event,
    eventId: event.id,
    data: event.data
  }
}

async function* toAsyncIterable<T>(items: T[]): AsyncIterable<T> {
  for (const item of items) yield item
}

function isTerminalCodexOutputLine(
  line: string,
  state: { sawFinalAnswerText?: boolean } = {}
): boolean {
  let payload: any
  try {
    payload = JSON.parse(line)
  } catch {
    return false
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return false
  if (payload.method === 'turn/completed') {
    return Boolean(terminalPayloadText(payload) || state.sawFinalAnswerText)
  }
  return (
    (payload.type === 'turn.completed' &&
      Boolean(terminalPayloadText(payload) || state.sawFinalAnswerText)) ||
    payload.type === 'turn.failed' ||
    (payload.type === 'turn.done' &&
      Boolean(terminalPayloadText(payload) || state.sawFinalAnswerText)) ||
    payload.method === 'error' ||
    payload.type === 'result'
  )
}

function outputLineCarriesFinalAnswerText(line: string): boolean {
  let payload: any
  try {
    payload = JSON.parse(line)
  } catch {
    return false
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return false
  if (payload.method === 'item/agentMessage/delta' || payload.type === 'item.agentMessage.delta') {
    return Boolean(terminalPayloadText(payload))
  }
  if (payload.type === 'assistant') return Boolean(terminalPayloadText(payload))
  return false
}

function terminalPayloadText(value: any): string {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  for (const key of ['result', 'result_text', 'text', 'final_text', 'message', 'delta', 'content']) {
    const nested = terminalPayloadText(value[key])
    if (nested.trim()) return nested
  }
  return ''
}

function sessionErrorMessage(event: SyntheticEvent): string {
  try {
    const payload = JSON.parse(event.data)
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      return typeof payload.error === 'string'
        ? payload.error
        : typeof payload.message === 'string'
          ? payload.message
          : event.event
    }
  } catch {
    if (event.data.trim()) return event.data.trim()
  }
  return event.event
}

function observedIssues(
  testCase: SyntheticCase,
  consumed: SyntheticEvent[],
  rendered: {
    chunks: ChatSDKStreamChunk[]
    outputs: ChatSDKOutput[]
  }
): string[] {
  const issues = new Set<string>()
  const markdown = rendered.chunks
    .filter((chunk): chunk is Extract<ChatSDKStreamChunk, { type: 'markdown_text' }> => {
      return chunk.type === 'markdown_text'
    })
    .map(chunk => chunk.text)
    .join('')
  const hasClosedError = rendered.outputs.some(output => {
    return output.type === 'chat.session.closed' && Boolean(output.message?.error)
  })

  if (!markdown.trim()) issues.add('no_markdown_text_chunk')
  if (consumed.length < testCase.events.length) issues.add('slackbot_parser_stopped_before_all_events')
  if (hasClosedError && !markdown.trim()) issues.add('closed_error_not_visible_as_markdown')
  if (
    !markdown.trim() &&
    rendered.chunks.some(chunk => chunk.type === 'task_update' && chunk.status === 'error')
  ) {
    issues.add('error_visible_only_as_task_status')
  }
  return [...issues].sort()
}
