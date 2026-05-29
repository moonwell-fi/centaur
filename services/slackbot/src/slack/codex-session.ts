import { createHash } from 'crypto'
import type { WebClient } from '@slack/web-api'
import type {
  AgentContentBlock,
  AgentMessagePayload,
  AgentStreamData,
  ToolResultEntry,
  ToolUseContentBlock
} from '@centaur/api-client'
import { slackReplyLimits } from '../constants'
import { logInfo } from '../logging'
import { AgentSessionRenderer } from './agent-session'
import {
  clipLines,
  preformatted as pre,
  richText,
  section,
  text,
  type StreamRichText,
  type StreamRichTextElement
} from './streaming'

const COMMAND_EXECUTION_TITLE = 'Command execution'

type AgentMessagePhase = 'commentary' | 'final_answer'
type CodexTextDelta = string | { text?: string; content?: string }
type CodexPlanItem = { step?: string; status?: string }
type CodexFileChange = {
  path?: string
  diff?: string
  unified_diff?: string
}
type CodexContent = CodexTextDelta | AgentContentBlock[] | ToolResultEntry[]

export type SlackbotHarnessEvent = AgentStreamData & {
  session_id?: string
  thread_id?: string
  centaur_thread_key?: string
  centaur_execution_id?: string
  centaur_assignment_generation?: number
  item?: CodexEventItem
  itemId?: string
  item_id?: string
  delta?: CodexTextDelta
  text?: string
  content?: CodexContent
  plan?: CodexPlanItem[]
  message?: AgentMessagePayload
}

type CodexEventItem = {
  id?: string
  itemId?: string
  type?: string
  phase?: string
  text?: string
  command?: string | null
  command_id?: string
  path?: string
  status?: string | null
  exitCode?: number | string | null
  exit_code?: number | string | null
  aggregated_output?: string | null
  aggregatedOutput?: string
  output?: string
  stdout?: string
  stderr?: string
  changes?: CodexFileChange[]
}

type HarnessTask = {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'complete' | 'error'
  details: StreamRichTextElement[]
  output: StreamRichTextElement[]
  commandIndex?: number
}

type CodexSessionState = {
  threadId: string
  stepCounter: number
  nextCommandIndex: number
  answerByItemId: Map<string, string>
  harnessAnswerText: string
  answerText: string
  commentaryByItemId: Map<string, string>
  harnessCommentaryText: string
  commentaryText: string
  completedItemIds: Set<string>
  firstBufferedTextAt: number | null
  streamedCommentaryText: string
  streamedAnswerText: string
  deliveredAnswerChars: number
  agentMessagePhase: AgentMessagePhase | null
  agentMessagePhaseByItemId: Map<string, AgentMessagePhase>
  planText: string
  taskByUseId: Map<string, HarnessTask>
  commandOutputById: Map<string, string>
  emittedActivityRunByTaskId: Set<string>
  emittedActivityOutputByTaskId: Set<string>
  done: boolean
}

type CompletedCodexSessionState = {
  threadId: string
  streamedAnswerChars: number
  completedAt: number
}

const states = new Map<string, CodexSessionState>()
const completedStates = new Map<string, CompletedCodexSessionState>()
const PRE_STREAM_GRACE_MS = 500
const COMPLETED_STATE_TTL_MS = 10 * 60 * 1000
const COMMAND_OUTPUT_KEYS = [
  'aggregated_output',
  'aggregatedOutput',
  'output',
  'stdout',
  'stderr'
] as const

export class CodexSessionRenderer {
  private readonly renderer: AgentSessionRenderer

  constructor(client: WebClient) {
    this.renderer = new AgentSessionRenderer(client)
  }

  async event(
    agentSessionId: string,
    event: SlackbotHarnessEvent
  ): Promise<{ threadId?: string; done: boolean; streamedAnswerChars: number }> {
    const completed = completedState(agentSessionId)
    if (completed) {
      if (isTerminalTurnEvent(event)) {
        logCodexTerminalEventIgnoredAfterDone(agentSessionId, event, completed)
      }
      return {
        threadId: completed.threadId || undefined,
        done: true,
        streamedAnswerChars: completed.streamedAnswerChars
      }
    }
    const state = getState(agentSessionId)
    if (event?.session_id) state.threadId = String(event.session_id)
    if (event?.thread_id) state.threadId = String(event.thread_id)

    trackAgentMessageLifecycle(event, state)
    ensureCommentarySegmentBreak(event, state)

    const structuredPlan = structuredPlanUpdate(event)
    if (structuredPlan) {
      await this.publishStructuredPlan(agentSessionId, state, structuredPlan)
    }

    const planText = planTextUpdate(event)
    if (planText) {
      state.planText = event?.type === 'item.plan.delta' ? state.planText + planText : planText
      await this.publishPlanText(agentSessionId, state, state.planText)
    }

    const command = commandExecution(event)
    if (command) {
      const id = commandId(command)
      const aggregatedOutput = commandAggregatedOutput(command)
      if (aggregatedOutput) state.commandOutputById.set(id, aggregatedOutput)
      const existing = state.taskByUseId.get(id)
      const commandIndex = commandNumber(state, existing)
      const task = commandTask(
        command,
        event?.type,
        existing,
        state.commandOutputById.get(id),
        commandIndex
      )
      const merged = mergeTask(existing, task)
      state.taskByUseId.set(merged.id, merged)
      await this.publishActivitySummary(agentSessionId, state)
    }

    const fileChange = fileChangeEvent(event)
    if (fileChange) {
      const existing = state.taskByUseId.get(fileChangeId(fileChange))
      const task = fileChangeTask(fileChange, event?.type, existing)
      const merged = mergeTask(existing, task)
      state.taskByUseId.set(merged.id, merged)
      await this.publishActivitySummary(agentSessionId, state)
    }

    const outputDelta = commandOutputDelta(event)
    if (outputDelta) {
      const current = state.commandOutputById.get(outputDelta.id) ?? ''
      const output = current + outputDelta.delta
      state.commandOutputById.set(outputDelta.id, output)
      const existing = state.taskByUseId.get(outputDelta.id)
      const commandIndex = commandNumber(state, existing)
      const task =
        existing ??
        ({
          id: outputDelta.id,
          title: commandExecutionTitle(commandIndex),
          status: 'in_progress',
          details: [],
          output: [],
          commandIndex
        } satisfies HarnessTask)
      const updated = {
        ...task,
        title: commandExecutionTitle(commandIndex),
        commandIndex,
        output: commandOutputElements(output)
      }
      state.taskByUseId.set(outputDelta.id, updated)
      await this.publishActivitySummary(agentSessionId, state)
    }

    for (const tool of toolUses(event)) {
      const commandIndex = tool.name === 'Bash' ? commandNumber(state) : undefined
      const task: HarnessTask = {
        id: `task-${++state.stepCounter}`,
        title: tool.name === 'Bash' ? commandExecutionTitle(commandIndex) : titleFor(tool),
        status: 'in_progress',
        details: detailElementsForTool(tool),
        output: [],
        ...(commandIndex !== undefined ? { commandIndex } : {})
      }
      state.taskByUseId.set(String(tool.id), task)
      await this.publishActivitySummary(agentSessionId, state)
    }

    for (const result of toolResults(event)) {
      const toolUseId = String(result.tool_use_id ?? '')
      const task = state.taskByUseId.get(toolUseId) ?? {
        id: `task-${++state.stepCounter}`,
        title: 'Tool result',
        status: 'in_progress',
        details: [],
        output: []
      }
      state.taskByUseId.set(toolUseId || task.id, task)
      task.status = 'complete'
      task.output = outputElementsForResult(result)
      await this.publishActivitySummary(agentSessionId, state)
    }

    if (eventCarriesAgentMessageText(event)) {
      const buffer = activeAssistantBuffer(state, event, agentSessionId)
      const update = applyAgentMessageUpdate(state, event, buffer, agentSessionId)
      if (update.bufferChanged) {
        await this.publishPendingAssistantText(agentSessionId, state)
      }
      if (update.correction) {
        logCanonicalCorrection(agentSessionId, event, state, update.correction)
      }
      if (buffer === 'commentary' && event?.type === 'item.completed') {
        upsertThinkingTask(state, event)
        await this.publishActivitySummary(agentSessionId, state)
      }
    }

    const reasoningMessage = reasoningText(event).trim()
    if (reasoningMessage) {
      const task: HarnessTask = {
        id: `reasoning-${++state.stepCounter}`,
        title: 'Thinking',
        status: 'complete',
        details: [section([text(reasoningMessage)])],
        output: []
      }
      state.taskByUseId.set(task.id, task)
      await this.publishActivitySummary(agentSessionId, state)
    }

    if (isTerminalTurnEvent(event)) {
      const resultText = terminalResultText(event)
      const willClose = Boolean(resultText || event?.type !== 'result')
      logCodexTerminalEventReceived(agentSessionId, event, state, {
        resultText,
        willClose
      })
      if (resultText && !state.answerText.trim()) {
        state.harnessAnswerText += resultText
        recomposeBuffers(state)
        await this.publishPendingAssistantText(agentSessionId, state, { force: true })
      }
      if (willClose) {
        await this.done(agentSessionId)
      }
    }

    return {
      threadId: state.threadId || undefined,
      done: state.done,
      streamedAnswerChars: state.deliveredAnswerChars
    }
  }

  async done(agentSessionId: string, threadId?: string): Promise<void> {
    const state = getState(agentSessionId)
    if (state.done) return
    if (threadId) state.threadId = threadId
    state.done = true
    completeThinkingTasks(state)
    completeOpenTasks(state)
    await this.publishActivitySummary(agentSessionId, state, { final: true })
    await this.publishPendingAssistantText(agentSessionId, state, { force: true })
    const { streamedTextChars } = await this.renderer.done(agentSessionId, {
      streamFinalUpdates: true,
      answerMarkdown: state.answerText
    })
    state.deliveredAnswerChars = streamedTextChars
    state.done = true
    completedStates.set(agentSessionId, {
      threadId: state.threadId,
      streamedAnswerChars: state.deliveredAnswerChars,
      completedAt: Date.now()
    })
    states.delete(agentSessionId)
  }

  private async publishActivitySummary(
    agentSessionId: string,
    state: CodexSessionState,
    opts: { final?: boolean } = {}
  ): Promise<void> {
    const tasks = Array.from(state.taskByUseId.values())
    if (!tasks.length) return
    for (const update of changedActivityTaskUpdates(state, tasks, opts)) {
      await this.renderer.step(
        agentSessionId,
        {
          id: update.id,
          title: update.title,
          status: update.status,
          details: update.details,
          output: update.output
        },
        { flush: true }
      )
    }
    await this.publishPendingAssistantText(agentSessionId, state)
  }

  private async publishPendingAssistantText(
    agentSessionId: string,
    state: CodexSessionState,
    opts: { force?: boolean } = {}
  ): Promise<void> {
    if (
      state.firstBufferedTextAt === null &&
      (state.commentaryText.trim() || state.answerText.trim())
    ) {
      state.firstBufferedTextAt = Date.now()
    }
    state.streamedCommentaryText = state.commentaryText
    const hasPlan = state.taskByUseId.size > 0
    const graceExpired =
      state.firstBufferedTextAt !== null &&
      Date.now() - state.firstBufferedTextAt >= PRE_STREAM_GRACE_MS
    const canStream = hasPlan || opts.force || graceExpired
    if (!canStream) return

    if (state.commentaryText.length > state.streamedCommentaryText.length) return
    if (state.answerText.length <= state.streamedAnswerText.length) return
    const delta = state.answerText.slice(state.streamedAnswerText.length)
    if (!delta) return
    const acceptedChars = await this.renderer.textDelta(agentSessionId, delta, {
      force: opts.force ?? false,
      planPrefix: hasPlan
    })
    if (acceptedChars > 0) {
      state.streamedAnswerText += delta.slice(0, acceptedChars)
      state.deliveredAnswerChars = this.renderer.streamedTextChars(agentSessionId)
    }
  }

  private async publishStructuredPlan(
    agentSessionId: string,
    state: CodexSessionState,
    plan: Array<{ step: string; status?: string }>
  ): Promise<void> {
    for (const [index, item] of plan.entries()) {
      setPlanTask(state, index, String(item.step ?? ''), planStatus(item.status))
    }
    await this.publishActivitySummary(agentSessionId, state)
  }

  private async publishPlanText(
    agentSessionId: string,
    state: CodexSessionState,
    value: string
  ): Promise<void> {
    const steps = parsePlanText(value)
    if (!steps.length) return
    for (const [index, item] of steps.entries()) {
      setPlanTask(state, index, item.step, item.status)
    }
    await this.publishActivitySummary(agentSessionId, state)
  }
}

export function hasActiveCodexSession(agentSessionId: string): boolean {
  const state = states.get(agentSessionId)
  return Boolean(state && !state.done)
}

function getState(agentSessionId: string): CodexSessionState {
  let state = states.get(agentSessionId)
  if (!state) {
    state = {
      threadId: '',
      stepCounter: 0,
      nextCommandIndex: 0,
      answerByItemId: new Map(),
      harnessAnswerText: '',
      answerText: '',
      commentaryByItemId: new Map(),
      harnessCommentaryText: '',
      commentaryText: '',
      completedItemIds: new Set(),
      firstBufferedTextAt: null,
      streamedCommentaryText: '',
      streamedAnswerText: '',
      deliveredAnswerChars: 0,
      agentMessagePhase: null,
      agentMessagePhaseByItemId: new Map(),
      planText: '',
      taskByUseId: new Map(),
      commandOutputById: new Map(),
      emittedActivityRunByTaskId: new Set(),
      emittedActivityOutputByTaskId: new Set(),
      done: false
    }
    states.set(agentSessionId, state)
  }
  return state
}

function content(event: SlackbotHarnessEvent): AgentContentBlock[] {
  return Array.isArray(event?.message?.content) ? event.message.content : []
}

function agentMessageItemPhase(item: CodexEventItem | undefined): AgentMessagePhase | null {
  const phase = String(item?.phase ?? '').toLowerCase()
  if (phase === 'commentary') return 'commentary'
  if (phase === 'final_answer' || phase === 'finalanswer') return 'final_answer'
  return null
}

function trackAgentMessageLifecycle(event: SlackbotHarnessEvent, state: CodexSessionState): void {
  if (event?.type !== 'item.started' && event?.type !== 'item.completed') return
  const phase = agentMessageItemPhase(event?.item)
  if (!phase) return
  state.agentMessagePhase = phase
  const id = agentMessageEventId(event)
  if (id) state.agentMessagePhaseByItemId.set(id, phase)
}

function agentMessageEventId(event: SlackbotHarnessEvent): string {
  return String(event?.itemId ?? event?.item_id ?? event?.item?.id ?? '')
}

/** Codex may emit several commentary agentMessages in one turn; keep a blank line between them. */
function ensureCommentarySegmentBreak(event: SlackbotHarnessEvent, state: CodexSessionState): void {
  if (event?.type !== 'item.started') return
  if (agentMessageItemPhase(event?.item) !== 'commentary') return
  const lastId = lastInsertedKey(state.commentaryByItemId)
  if (lastId) {
    const prior = state.commentaryByItemId.get(lastId) ?? ''
    if (prior.trim() && !prior.endsWith('\n\n')) {
      state.commentaryByItemId.set(lastId, prior.endsWith('\n') ? `${prior}\n` : `${prior}\n\n`)
    }
  } else if (state.harnessCommentaryText.trim() && !state.harnessCommentaryText.endsWith('\n\n')) {
    state.harnessCommentaryText = state.harnessCommentaryText.endsWith('\n')
      ? `${state.harnessCommentaryText}\n`
      : `${state.harnessCommentaryText}\n\n`
  } else {
    return
  }
  recomposeBuffers(state)
}

function lastInsertedKey<K>(map: Map<K, unknown>): K | undefined {
  let last: K | undefined
  for (const key of map.keys()) last = key
  return last
}

function commentaryItemId(event: SlackbotHarnessEvent): string {
  return String(event?.itemId ?? event?.item_id ?? event?.item?.id ?? '')
}

function upsertThinkingTask(state: CodexSessionState, event: SlackbotHarnessEvent): void {
  const id = commentaryItemId(event)
  if (!id) return
  const body = stringValue(event?.item?.text) || state.commentaryByItemId.get(id) || ''
  upsertThinkingTaskFromBody(state, id, body)
}

function upsertThinkingTaskFromBody(state: CodexSessionState, id: string, body: string): void {
  const trimmed = body.trim()
  if (!trimmed) return
  const taskId = `thinking-${id}`
  if (state.commentaryByItemId.get(id) !== trimmed) {
    state.commentaryByItemId.set(id, trimmed)
    recomposeBuffers(state)
  }
  state.taskByUseId.set(taskId, {
    id: taskId,
    title: 'Thinking',
    status: 'complete',
    details: [section([text(trimmed)])],
    output: []
  })
}

function completeThinkingTasks(state: CodexSessionState): void {
  for (const [id, body] of state.commentaryByItemId) {
    upsertThinkingTaskFromBody(state, id, body)
  }
}

function activeAssistantBuffer(
  state: CodexSessionState,
  event: SlackbotHarnessEvent,
  agentSessionId: string
): 'commentary' | 'answer' {
  if (event?.type === 'item.agentMessage.delta' || event?.type === 'item.completed') {
    const codexId = agentMessageEventId(event)
    const itemPhase = state.agentMessagePhaseByItemId.get(codexId)
    if (itemPhase) return itemPhase === 'final_answer' ? 'answer' : 'commentary'
    if (
      event?.type === 'item.completed' &&
      (event?.item?.type === 'agentMessage' || event?.item?.type === 'agent_message') &&
      state.taskByUseId.size > 0
    ) {
      logInfo('slack_codex_unphased_final_agent_message_classified', {
        agent_session_id: agentSessionId,
        centaur_thread_key: event?.centaur_thread_key,
        execution_id: event?.centaur_execution_id,
        assignment_generation: event?.centaur_assignment_generation,
        codex_id: codexId,
        codex_item_id: codexId,
        codex_item_type: event?.item?.type,
        codex_session_id: state.threadId || event?.session_id || event?.thread_id,
        task_count: state.taskByUseId.size,
        commentary_chars: state.commentaryText.length,
        answer_chars: state.answerText.length,
        item_text_chars: String(event?.item?.text ?? '').length
      })
      return 'answer'
    }
    return state.agentMessagePhase === 'final_answer' ? 'answer' : 'commentary'
  }
  return 'answer'
}

function eventCarriesAgentMessageText(event: SlackbotHarnessEvent): boolean {
  if (event?.type === 'item.agentMessage.delta') return Boolean(extractDeltaText(event))
  if (event?.type === 'assistant') return Boolean(assistantTextFromAssistantEvent(event))
  if (event?.type === 'item.completed') {
    const itemType = event?.item?.type
    if (itemType !== 'agentMessage' && itemType !== 'agent_message') return false
    return Boolean(String(event?.item?.text ?? ''))
  }
  return false
}

type AgentMessageUpdateResult = {
  bufferChanged: boolean
  correction?: { previous: string; canonical: string }
}

function applyAgentMessageUpdate(
  state: CodexSessionState,
  event: SlackbotHarnessEvent,
  buffer: 'answer' | 'commentary',
  agentSessionId: string
): AgentMessageUpdateResult {
  const itemId = agentMessageEventId(event)

  if (event?.type === 'item.agentMessage.delta') {
    if (!itemId || state.completedItemIds.has(itemId)) return { bufferChanged: false }
    const delta = extractDeltaText(event)
    if (!delta) return { bufferChanged: false }
    const byId = buffer === 'answer' ? state.answerByItemId : state.commentaryByItemId
    byId.set(itemId, (byId.get(itemId) ?? '') + delta)
    recomposeBuffers(state)
    return { bufferChanged: true }
  }

  if (event?.type === 'item.completed') {
    const canonical = String(event?.item?.text ?? '')
    if (!canonical) return { bufferChanged: false }
    if (!itemId) {
      // Without an item id we cannot map this canonical text to the per-item buffer it
      // was meant to replace. Falling back to a flat append would re-introduce the
      // pre-fix duplication, so we drop and log instead. Codex always emits an id in
      // observed prod streams; this branch defends against malformed events.
      logInfo('slack_codex_item_completed_missing_id', {
        agent_session_id: agentSessionId,
        centaur_thread_key: event?.centaur_thread_key,
        execution_id: event?.centaur_execution_id,
        canonical_text_chars: canonical.length,
        canonical_hash: textHash(canonical)
      })
      return { bufferChanged: false }
    }
    const byId = buffer === 'answer' ? state.answerByItemId : state.commentaryByItemId
    const previous = byId.get(itemId) ?? ''
    state.completedItemIds.add(itemId)
    if (canonical === previous) return { bufferChanged: false }
    byId.set(itemId, canonical)
    recomposeBuffers(state)
    return {
      bufferChanged: true,
      correction: previous ? { previous, canonical } : undefined
    }
  }

  if (event?.type === 'assistant') {
    const text = assistantTextFromAssistantEvent(event)
    if (!text) return { bufferChanged: false }
    const key = buffer === 'answer' ? 'harnessAnswerText' : 'harnessCommentaryText'
    const before = state[key]
    if (text === before || before.endsWith(text)) return { bufferChanged: false }
    if (assistantEventLooksCanonical(event)) {
      state[key] = text
    } else if (text.startsWith(before)) {
      state[key] = text
    } else {
      state[key] = before + text
    }
    recomposeBuffers(state)
    return { bufferChanged: true }
  }

  return { bufferChanged: false }
}

function recomposeBuffers(state: CodexSessionState): void {
  state.answerText = compose(state.answerByItemId, state.harnessAnswerText)
  state.commentaryText = compose(state.commentaryByItemId, state.harnessCommentaryText)
}

function compose(byItemId: Map<string, string>, trailing: string): string {
  let out = ''
  for (const value of byItemId.values()) out += value
  return trailing ? out + trailing : out
}

function extractDeltaText(event: any): string {
  const delta = event?.delta ?? event?.text ?? event?.content ?? ''
  if (delta && typeof delta === 'object') return String(delta.text ?? delta.content ?? '')
  return String(delta)
}

function assistantTextFromAssistantEvent(event: any): string {
  return content(event)
    .map(part => (part.type === 'text' ? part.text : ''))
    .filter(Boolean)
    .join('')
}

function assistantEventLooksCanonical(event: any): boolean {
  const message = event?.message
  return Boolean(
    event?.uuid ||
    event?.request_id ||
    event?.session_id ||
    message?.id ||
    message?.model ||
    message?.usage
  )
}

function logCanonicalCorrection(
  agentSessionId: string,
  event: SlackbotHarnessEvent,
  state: CodexSessionState,
  correction: { previous: string; canonical: string }
): void {
  const { previous, canonical } = correction
  const charsDiff = canonical.length - previous.length
  logInfo('slack_codex_canonical_answer_correction', {
    agent_session_id: agentSessionId,
    centaur_thread_key: event?.centaur_thread_key,
    execution_id: event?.centaur_execution_id,
    assignment_generation: event?.centaur_assignment_generation,
    event_type: event?.type,
    codex_id: agentMessageEventId(event),
    codex_item_id: agentMessageEventId(event),
    codex_item_type: event?.item?.type,
    codex_item_phase: event?.item?.phase,
    codex_session_id: state.threadId || event?.session_id || event?.thread_id,
    delta_total_chars: previous.length,
    canonical_text_chars: canonical.length,
    chars_diff: charsDiff,
    delta_hash: textHash(previous),
    canonical_hash: textHash(canonical),
    streamed_answer_chars: state.deliveredAnswerChars
  })
}

function textHash(value: string): string {
  return createHash('sha256').update(value).digest('hex').slice(0, 16)
}

function reasoningText(event: SlackbotHarnessEvent): string {
  if (event?.type !== 'reasoning') return ''
  return String(event.text ?? event.thinking ?? '')
}

function isTerminalTurnEvent(event: SlackbotHarnessEvent): boolean {
  return event?.type === 'result' || event?.type === 'turn.done' || event?.type === 'turn.completed'
}

function terminalResultText(event: SlackbotHarnessEvent): string {
  for (const key of ['result', 'result_text', 'text', 'final_text']) {
    const value = event?.[key]
    if (typeof value !== 'string') continue
    const text = value.trim()
    if (text) return text
  }
  return ''
}

function completedState(agentSessionId: string): CompletedCodexSessionState | undefined {
  const completed = completedStates.get(agentSessionId)
  if (!completed) return undefined
  if (Date.now() - completed.completedAt > COMPLETED_STATE_TTL_MS) {
    completedStates.delete(agentSessionId)
    return undefined
  }
  return completed
}

function logCodexTerminalEventReceived(
  agentSessionId: string,
  event: SlackbotHarnessEvent,
  state: CodexSessionState,
  opts: { resultText: string; willClose: boolean }
): void {
  logInfo('slack_codex_terminal_event_received', {
    agent_session_id: agentSessionId,
    centaur_thread_key: event?.centaur_thread_key,
    execution_id: event?.centaur_execution_id,
    assignment_generation: event?.centaur_assignment_generation,
    event_type: event?.type,
    codex_session_id: state.threadId || event?.session_id || event?.thread_id,
    already_completed: false,
    will_close: opts.willClose,
    result_text_chars: opts.resultText.length,
    answer_chars_before_event: state.answerText.length,
    streamed_answer_chars_before_event: state.deliveredAnswerChars,
    task_count: state.taskByUseId.size
  })
}

function logCodexTerminalEventIgnoredAfterDone(
  agentSessionId: string,
  event: SlackbotHarnessEvent,
  completed: CompletedCodexSessionState
): void {
  logInfo('slack_codex_terminal_event_ignored_after_done', {
    agent_session_id: agentSessionId,
    centaur_thread_key: event?.centaur_thread_key,
    execution_id: event?.centaur_execution_id,
    assignment_generation: event?.centaur_assignment_generation,
    event_type: event?.type,
    codex_session_id: completed.threadId || event?.session_id || event?.thread_id,
    already_completed: true,
    will_close: false,
    result_text_chars: terminalResultText(event).length,
    streamed_answer_chars_at_completion: completed.streamedAnswerChars,
    completed_age_ms: Date.now() - completed.completedAt
  })
}

function toolUses(event: SlackbotHarnessEvent): ToolUseContentBlock[] {
  if (event?.type !== 'assistant') return []
  const toolUses: ToolUseContentBlock[] = []
  for (const part of content(event)) {
    try {
      toolUses.push(assertToolUseContentBlock(part))
    } catch {
      // ignore non-tool content blocks
    }
  }
  return toolUses
}

function toolResults(event: SlackbotHarnessEvent): ToolResultEntry[] {
  if (event?.type !== 'user' && event?.type !== 'tool') return []
  const direct = Array.isArray(event?.content) ? event.content : []
  const results: ToolResultEntry[] = []
  for (const part of direct) {
    try {
      results.push(assertToolResultEntry(part))
    } catch {
      // ignore non-tool-result content blocks
    }
  }
  return results
}

function commandExecution(event: SlackbotHarnessEvent): CodexEventItem | null {
  if (event?.type === 'command_execution') return event
  if (
    event?.type !== 'item.started' &&
    event?.type !== 'item.updated' &&
    event?.type !== 'item.completed'
  )
    return null
  const item = event.item
  if (!item || (item.type !== 'commandExecution' && item.type !== 'command_execution')) return null
  return item
}

function fileChangeEvent(event: SlackbotHarnessEvent): CodexEventItem | null {
  if (event?.type === 'file_change') return event
  if (
    event?.type !== 'item.started' &&
    event?.type !== 'item.updated' &&
    event?.type !== 'item.completed'
  )
    return null
  const item = event.item
  if (!item || (item.type !== 'fileChange' && item.type !== 'file_change')) return null
  return item
}

function structuredPlanUpdate(event: SlackbotHarnessEvent): Array<{ step: string; status?: string }> | null {
  if (event?.type !== 'turn.plan.updated') return null
  if (!Array.isArray(event.plan)) return null
  return event.plan
    .map(item => ({
      step: stringValue(item.step),
      ...(typeof item.status === 'string' ? { status: item.status } : {})
    }))
    .filter(item => item.step)
}

function planTextUpdate(event: SlackbotHarnessEvent): string {
  if (event?.type === 'item.plan.delta') {
    return String(event.delta ?? event.text ?? '')
  }
  if (event?.type === 'item.completed' && event?.item?.type === 'plan') {
    return String(event.item.text ?? '')
  }
  return ''
}

function parsePlanText(value: string): Array<{ step: string; status: HarnessTask['status'] }> {
  return value
    .split('\n')
    .map(line => {
      const trimmed = line.trim()
      if (!/^[-*]\s+|\d+[.)]\s+/.test(trimmed)) return null
      return {
        step: trimmed,
        status: /\[[xX]\]/.test(trimmed) ? ('complete' as const) : ('pending' as const)
      }
    })
    .filter(item => item !== null)
}

function planStatus(value: string | undefined): HarnessTask['status'] {
  const status = String(value ?? '').toLowerCase()
  if (status === 'inprogress' || status === 'in_progress' || status === 'running')
    return 'in_progress'
  if (status === 'completed' || status === 'complete' || status === 'done') return 'complete'
  if (status === 'failed' || status === 'error') return 'complete'
  return 'pending'
}

function stripPlanMarker(value: string): string {
  return value
    .replace(/^\s*(?:[-*]|\d+[.)])\s+/, '')
    .replace(/^\[[ xX]\]\s+/, '')
    .trim()
}

function setPlanTask(
  state: CodexSessionState,
  index: number,
  step: string,
  status: HarnessTask['status']
): void {
  const title = oneLine(stripPlanMarker(step), slackReplyLimits.stream.planTitleChars)
  if (!title) return
  state.taskByUseId.set(`plan-${index + 1}`, {
    id: `plan-${index + 1}`,
    title,
    status,
    details: [],
    output: []
  })
}

function completeOpenTasks(state: CodexSessionState): void {
  for (const [id, task] of state.taskByUseId) {
    if (task.status !== 'in_progress' && task.status !== 'pending') continue
    state.taskByUseId.set(id, { ...task, status: 'complete' })
  }
}

function changedActivityTaskUpdates(
  state: CodexSessionState,
  tasks: HarnessTask[],
  opts: { final?: boolean } = {}
): Array<{
  id: string
  title: string
  status: HarnessTask['status']
  details?: StreamRichText
  output?: StreamRichText
}> {
  const updates: Array<{
    id: string
    title: string
    status: HarnessTask['status']
    details?: StreamRichText
    output?: StreamRichText
  }> = []
  for (const task of tasks) {
    let details: StreamRichText | undefined
    let output: StreamRichText | undefined
    if (opts.final) {
      if (task.details.length && !state.emittedActivityRunByTaskId.has(task.id)) {
        state.emittedActivityRunByTaskId.add(task.id)
        details = activityRunBlock(task)
      }
      if (task.output.length && !state.emittedActivityOutputByTaskId.has(task.id)) {
        state.emittedActivityOutputByTaskId.add(task.id)
        output = activityOutputBlock(task)
      }
    } else if (task.details.length && !state.emittedActivityRunByTaskId.has(task.id)) {
      state.emittedActivityRunByTaskId.add(task.id)
      details = activityRunBlock(task)
    }
    if (
      !opts.final &&
      task.status === 'complete' &&
      task.output.length &&
      !state.emittedActivityOutputByTaskId.has(task.id)
    ) {
      state.emittedActivityOutputByTaskId.add(task.id)
      output = activityOutputBlock(task)
    }
    if (!details && !output && !opts.final) continue
    updates.push({
      id: task.id,
      title: task.title,
      status: task.status,
      details,
      output
    })
  }
  return updates
}

function activityRunBlock(task: HarnessTask): StreamRichText {
  if (task.title === 'Thinking' && task.details.length) {
    return richText(task.details)
  }
  const command = firstPreformattedBody(task.details)
  if (command) {
    return richText([pre(command, shellLanguage(firstPreformattedLanguage(task.details)))])
  }
  return richText(task.details)
}

function activityOutputBlock(task: HarnessTask): StreamRichText {
  return richText([
    pre(elementsToPlainText(task.output), firstPreformattedLanguage(task.output) ?? 'text')
  ])
}

function firstPreformattedBody(elements: StreamRichTextElement[]): string {
  return (
    elements
      .find(element => element.type === 'rich_text_preformatted')
      ?.elements.map(inline => inline.text ?? '')
      .join('') ?? ''
  )
}

function firstPreformattedLanguage(elements: StreamRichTextElement[]): string | undefined {
  return elements.find(element => element.type === 'rich_text_preformatted')?.language
}

function shellLanguage(language: string | undefined): string {
  return language === 'bash' || !language ? 'sh' : language
}

function shellLanguageForCommand(_command: string): string {
  return 'sh'
}

function commandOutputDelta(event: SlackbotHarnessEvent): { id: string; delta: string } | null {
  if (event?.type !== 'item.commandExecution.outputDelta') return null
  const id = String(event.itemId ?? event.item_id ?? '')
  const delta = String(event.delta ?? '')
  return id && delta ? { id, delta } : null
}

function commandId(item: CodexEventItem): string {
  return String(item.id ?? item.itemId ?? item.command_id ?? item.command ?? 'command')
}

function fileChangeId(item: CodexEventItem): string {
  return String(item.id ?? item.itemId ?? item.path ?? 'file-change')
}

function commandNumber(state: CodexSessionState, existing?: HarnessTask): number {
  if (existing?.commandIndex !== undefined) return existing.commandIndex
  state.nextCommandIndex += 1
  return state.nextCommandIndex
}

function commandTask(
  item: CodexEventItem,
  eventType: string,
  existing?: HarnessTask,
  accumulatedOutput?: string,
  commandIndex?: number
): HarnessTask {
  const id = commandId(item)
  const rawCommand = String(item.command ?? 'Command')
  const displayCommand =
    rawCommand === 'Command' ? rawCommand : oneLine(unwrapShellCommand(rawCommand), 220)
  const status = commandStatus(item, eventType)
  const exitCode = item.exitCode ?? item.exit_code
  const failed = isCommandFailure(item, eventType)
  const isCompletionUpdate =
    eventType === 'item.completed' || status === 'complete' || status === 'error'
  const output = commandOutputElements(accumulatedOutput ?? '', exitCode)
  return {
    id,
    title: commandExecutionTitle(commandIndex),
    status,
    ...(commandIndex !== undefined ? { commandIndex } : {}),
    details:
      isCompletionUpdate && existing && !failed
        ? []
        : [pre(displayCommand, shellLanguageForCommand(displayCommand))],
    output
  }
}

function commandAggregatedOutput(item: CodexEventItem): string {
  for (const key of COMMAND_OUTPUT_KEYS) {
    const value = item?.[key]
    if (typeof value === 'string' && value) return value
  }
  return ''
}

function commandOutputElements(
  output: string,
  exitCode?: number | string | null
): StreamRichTextElement[] {
  const elements: StreamRichTextElement[] = []
  const normalizedOutput =
    exitCode !== null && exitCode !== undefined && exitCode !== 0
      ? `exit code ${exitCode}${output ? `\n${output}` : ''}`
      : output
  if (normalizedOutput) {
    const formatted = formatCommandOutput(normalizedOutput)
    elements.push(pre(formatted.body, formatted.language))
  }
  return elements
}

function formatCommandOutput(output: string): { body: string; language: string } {
  const trimmed = output.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      const pretty = JSON.stringify(JSON.parse(trimmed), null, 2)
      return {
        body: clipLines(pretty, slackReplyLimits.finalPlan.taskOutputCodeBlockLines),
        language: 'json'
      }
    } catch {}
  }
  return {
    body: clipLines(output, slackReplyLimits.finalPlan.taskOutputCodeBlockLines),
    language: languageFromContent(output)
  }
}

function fileChangeTask(
  item: CodexEventItem,
  eventType: string,
  existing?: HarnessTask
): HarnessTask {
  const id = fileChangeId(item)
  const changes = Array.isArray(item.changes) ? item.changes : []
  const paths = changes.map(change => stringValue(change.path)).filter(Boolean)
  const uniquePaths: string[] = Array.from(new Set(paths))
  const diff = changes
    .map(change => firstNonEmptyString(change.diff, change.unified_diff))
    .filter(Boolean)
    .join('\n\n')
  return {
    id,
    title:
      uniquePaths.length === 1
        ? `Edit ${uniquePaths[0]}`
        : uniquePaths.length > 1
          ? `Edit ${uniquePaths.length} files`
          : 'Apply file changes',
    status: itemStatus(item, eventType),
    details: uniquePaths.length
      ? [section([text('Files: '), text(uniquePaths.join(', '), { code: true })])]
      : (existing?.details ?? []),
    output: diff ? [pre(clip(diff), 'diff')] : (existing?.output ?? [])
  }
}

function mergeTask(existing: HarnessTask | undefined, update: HarnessTask): HarnessTask {
  return {
    ...update,
    details: update.details.length ? update.details : (existing?.details ?? []),
    output: update.output.length ? update.output : (existing?.output ?? [])
  }
}

function commandStatus(item: CodexEventItem, eventType: string): HarnessTask['status'] {
  if (isCommandFailure(item, eventType)) return 'complete'
  return itemStatus(item, eventType, item.exitCode ?? item.exit_code)
}

function isCommandFailure(item: CodexEventItem, eventType: string): boolean {
  const status = String(item.status ?? '').toLowerCase()
  const exitCode = item.exitCode ?? item.exit_code
  return (
    status === 'failed' ||
    (eventType === 'item.completed' &&
      exitCode !== 0 &&
      exitCode !== null &&
      exitCode !== undefined)
  )
}

function itemStatus(
  item: CodexEventItem,
  eventType: string,
  _exitCode?: number | string | null
): HarnessTask['status'] {
  const status = String(item.status ?? '').toLowerCase()
  if (status === 'failed' || status === 'declined') return 'complete'
  if (status === 'completed' || eventType === 'item.completed') {
    return 'complete'
  }
  return 'in_progress'
}

function elementsToPlainText(elements: StreamRichTextElement[]): string {
  return elements.map(elementToPlainText).filter(Boolean).join('\n')
}

function elementToPlainText(element: StreamRichTextElement): string {
  if (element.type === 'rich_text_preformatted') {
    const body = element.elements?.map(inline => inline.text ?? '').join('') ?? ''
    return body
  }
  if (element.type === 'rich_text_section') {
    return (element.elements ?? [])
      .map(inline => {
        if ('url' in inline) return inline.text ?? inline.url
        if ('user_id' in inline) return `<@${inline.user_id}>`
        return inline.text ?? ''
      })
      .join('')
  }
  return ''
}

function titleFor(tool: ToolUseContentBlock): string {
  if (tool.name === 'create_file') return 'Create file'
  if (tool.name === 'edit_file') return 'Edit file'
  return `Use ${tool.name ?? 'tool'}`
}

function detailElementsForTool(tool: ToolUseContentBlock): StreamRichTextElement[] {
  if (tool.name === 'Bash') {
    const command = oneLine(unwrapShellCommand(bashCommand(tool.input)), 220)
    return [pre(command, shellLanguageForCommand(command))]
  }
  if (tool.name === 'create_file') {
    const path = stringInput(tool.input, 'path', 'file')
    return [
      section([text('Created '), text(path, { code: true })]),
      pre(stringInput(tool.input, 'content'), languageFromPath(path))
    ]
  }
  if (tool.name === 'edit_file') {
    const path = stringInput(tool.input, 'path', 'file')
    const newStr = stringInput(tool.input, 'new_str')
    const diff = stringInput(tool.input, 'diff')
    const fileContent = stringInput(tool.input, 'content')
    if (newStr)
      return [
        section([text('Edited '), text(path, { code: true })]),
        pre(newStr, languageFromPath(path))
      ]
    if (diff)
      return [section([text('Edited '), text(path, { code: true })]), pre(stripFence(diff), 'diff')]
    if (fileContent)
      return [
        section([text('Edited '), text(path, { code: true })]),
        pre(fileContent, languageFromPath(path))
      ]
    return [section([text('Edited '), text(path, { code: true })])]
  }
  if (tool.name === 'Read') {
    return [
      section([
        text('Read '),
        text(stringInput(tool.input, 'file_path', stringInput(tool.input, 'path', 'file')), {
          code: true
        })
      ])
    ]
  }
  return [pre(JSON.stringify(tool.input ?? {}, null, 2), 'json')]
}

function outputElementsForResult(result: ToolResultEntry): StreamRichTextElement[] {
  let raw = result.content ?? ''
  if (Array.isArray(raw))
    raw = raw
      .map(part => {
        if (typeof part === 'string') return part
        try {
          const record = assertRecord(part)
          return stringValue(record.text) || JSON.stringify(record)
        } catch {
          return JSON.stringify(part)
        }
      })
      .join('\n')
  let rawText = String(raw ?? '')
  try {
    const parsed = assertCommandResultPayload(JSON.parse(rawText) as unknown)
    if (typeof parsed.diff === 'string') return [pre(stripFence(parsed.diff), 'diff')]
    if (parsed.output !== undefined)
      rawText =
        typeof parsed.output === 'string' && parsed.output
          ? parsed.output
          : `exitCode ${parsed.exitCode}`
  } catch {}
  const formatted = formatCommandOutput(rawText)
  if (formatted.body.includes('\n') || result.is_error) {
    return [pre(formatted.body, formatted.language)]
  }
  return [section([text(oneLine(rawText || 'Done'))])]
}

function textFromCodexDelta(input: unknown): string {
  if (typeof input === 'string') return input
  try {
    const record = assertRecord(input)
    return stringValue(record.text) || stringValue(record.content)
  } catch {
    return String(input ?? '')
  }
}

function stringValue(value: unknown): string {
  try {
    return assertString(value)
  } catch {
    return ''
  }
}

function firstNonEmptyString(...values: Array<string | undefined>): string {
  for (const value of values) {
    const text = stringValue(value).trim()
    if (text) return text
  }
  return ''
}

function assertToolUseContentBlock(value: unknown): ToolUseContentBlock {
  const record = assertRecord(value)
  if (record.type !== 'tool_use') throw new Error('expected tool_use content block')
  if (typeof record.id !== 'string') throw new Error('tool_use id must be a string')
  if (typeof record.name !== 'string') throw new Error('tool_use name must be a string')
  const input = assertRecord(record.input)
  return { ...record, type: 'tool_use', id: record.id, name: record.name, input }
}

function assertToolResultEntry(value: unknown): ToolResultEntry {
  const record = assertRecord(value)
  if (typeof record.tool_use_id !== 'string') {
    throw new Error('tool_result tool_use_id must be a string')
  }
  return {
    ...record,
    tool_use_id: record.tool_use_id,
    content: record.content,
    is_error: typeof record.is_error === 'boolean' ? record.is_error : undefined
  }
}

type CommandResultPayload = {
  diff?: string
  output?: string
  exitCode?: string | number | boolean | null
}

function assertCommandResultPayload(value: unknown): CommandResultPayload {
  const record = assertRecord(value)
  return {
    diff: optionalString(record.diff, 'diff'),
    output: optionalString(record.output, 'output'),
    exitCode: optionalScalar(record.exitCode, 'exitCode')
  }
}

function optionalString(value: unknown, field: string): string | undefined {
  if (value === undefined) return undefined
  if (typeof value !== 'string') throw new Error(`${field} must be a string`)
  return value
}

function optionalScalar(value: unknown, field: string): string | number | boolean | null | undefined {
  if (value === undefined) return undefined
  if (
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean' ||
    value === null
  ) {
    return value
  }
  throw new Error(`${field} must be a scalar`)
}

function assertString(value: unknown): string {
  if (typeof value !== 'string') throw new Error('expected string')
  return value
}

function assertRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('expected object')
  }
  return value as Record<string, unknown>
}

function stripFence(value: string): string {
  return value
    .trim()
    .replace(/^```[a-zA-Z0-9_-]*\n?/, '')
    .replace(/\n?```$/, '')
}

function bashCommand(input: Record<string, unknown>): string {
  return stringInput(input, 'command', stringInput(input, 'cmd'))
}

function stringInput(input: Record<string, unknown>, key: string, fallback = ''): string {
  const value = input?.[key]
  return typeof value === 'string' ? value : fallback
}

function languageFromPath(path: string): string {
  const name = path.split('/').pop() ?? ''
  const extension = name.includes('.') ? name.split('.').pop() : ''
  return extension?.toLowerCase() || 'text'
}

function languageFromContent(value: string): string {
  const trimmed = value.trim()
  if (
    /^(export\s+)?(async\s+)?function\s|^type\s+\w+\s*=|^interface\s+\w+|^const\s+\w+\s*[:=]/m.test(
      trimmed
    )
  )
    return 'ts'
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) return 'json'
  return 'text'
}

function clip(value: string, max: number = slackReplyLimits.finalPlan.outputPreviewChars): string {
  return value.length > max ? `${value.slice(0, max)}\n/* truncated */` : value
}

function oneLine(value: string, max: number = slackReplyLimits.finalPlan.taskTitleChars): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length > max ? `${normalized.slice(0, max - 1)}…` : normalized
}

/** Strip harness wrappers like `/bin/bash -lc 'call tools'`. */
function unwrapShellCommand(command: string): string {
  const trimmed = command.trim()
  if (!trimmed) return trimmed

  const bashLc = /^\/bin\/bash\s+-lc\s+([\s\S]+)$/i.exec(trimmed)
  if (!bashLc?.[1]) return trimmed

  let inner = bashLc[1].trim()
  if (
    (inner.startsWith("'") && inner.endsWith("'")) ||
    (inner.startsWith('"') && inner.endsWith('"'))
  ) {
    inner = inner.slice(1, -1)
  }
  return inner.trim() || trimmed
}

function commandExecutionTitle(index?: number): string {
  return index !== undefined ? `${index}. ${COMMAND_EXECUTION_TITLE}` : COMMAND_EXECUTION_TITLE
}
