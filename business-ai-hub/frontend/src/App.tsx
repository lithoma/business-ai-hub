import { ConditionSQLFields } from './ConditionSQLFields'
import { applySQLSuggestion, sqlGenerationRequest, type SQLSuggestion } from './taskConditions'
import { conversationContext, isTaskCreationIntent, type TaskSeed } from './taskConversation'
import { TaskProposalCard } from './TaskProposalCard'
import { WorkflowInspector, type WorkflowTrace } from './WorkflowInspector'
import { EmailFields } from './EmailFields'
import { Reports, TaskLifecycle } from './executionUi'
import { TaskTrial, type TrialSettings } from './ManualTrial'
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { EMPTY_FORM, WEEKDAYS, buildCreatePayload, formError, proposalToForm, scheduleDescription, taskCounts, type DraftConfiguration, type DraftReview, type ScheduleFreq, type TaskFormState, type TriggerType } from './taskDrafts'
import {
  Activity, ArrowRight, BarChart3, CalendarDays, Check, ChevronDown,
  Clock3, ClipboardList, FileText, Menu, MessageSquareText,
  Plus, RefreshCcw, Send, ShieldCheck, Sparkles, Target, X,
} from 'lucide-react'

type Page = 'chat' | 'tasks' | 'reports'
type Agent = 'genie_one'
type ChatMessage = { id: string; role: 'user' | 'assistant'; text: string; agent?: Agent; provider?: string; source?: 'system' | 'welcome'; task?: TaskSeed; traceId?: string }
type ProposalOrigin = { agent: Agent; messageId: string }
type ApiTask = Record<string, unknown>

type ChatResponse = {
  agent?: string
  answer?: string
  status?: string
  conversation_id?: string
  provider?: string
  analysis_ms?: number
}

type WorkflowPlan = {
  intent: 'analysis' | 'task' | 'mixed' | 'clarify'
  execution_order: 'independent' | 'analysis_first'
  clarification: string
  task_requirements?: string
  analysis_question?: string
  analysis_agent?: Agent
  analysis_ticket?: string
  configuration?: DraftConfiguration
  metrics?: { routing_ms?: number; usage?: { total_tokens?: number } }
}

type TasksResponse = { tasks?: ApiTask[]; task_count?: number; yaml_count?: number; registry_count?: number }

const AGENTS: Record<Agent, { name: string; short: string; subtitle: string; icon: 'sales' | 'market' }> = {
  genie_one: { name: 'Genie One', short: 'Genie One', subtitle: '按你的权限选择指标口径与业务数据', icon: 'sales' },
}
const SUGGESTIONS: Record<Agent, string[]> = {
  genie_one: ['2025年6月善存的GMV是多少？请注明指标口径。',
    '按月份总结2025年上半年的销售趋势。', '分析2025年上半年主要品牌的竞争格局。',
    '每周五给我一份竞品周报。'],
}

const API_TIMEOUT_MS = 8 * 60 * 1000 + 15000

function toErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (Array.isArray(detail)) return detail.map(item => `${Array.isArray(item.loc) ? item.loc.slice(1).join('.') : ''}: ${item.msg || '字段无效'}`).join('；')
  }
  return fallback
}

async function apiRequest<T>(url: string, options?: RequestInit, timeoutMs = 30000): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, {
      ...options,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...options?.headers },
      signal: controller.signal,
    })
    const raw = await response.text()
    let body: unknown
    try { body = raw ? JSON.parse(raw) : null } catch { body = null }
    if (!response.ok) {
      throw new Error(toErrorMessage(body, `请求失败（HTTP ${response.status}）`))
    }
    return body as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('请求等待超时，请稍后重试。')
    }
    if (error instanceof TypeError) {
      throw new Error('无法连接服务器，请检查网络或应用状态。')
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}

function traceHeaders(traceId?: string): Record<string,string> {
  return traceId ? { 'X-Hub-Trace-Id': traceId } : {}
}

function getString(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : ''
}

function taskField(task: ApiTask, ...keys: string[]): string {
  for (const key of keys) {
    const value = task[key]
    if (value !== null && value !== undefined && value !== '') {
      if (typeof value === 'string' || typeof value === 'number') return String(value)
      if (typeof value === 'object' && value && 'type' in value) return getString((value as ApiTask).type)
    }
  }
  return ''
}

function taskEnabled(task: ApiTask): boolean | null {
  const value = task.enabled ?? task.is_enabled ?? (task.config && typeof task.config === 'object' ? (task.config as ApiTask).enabled : undefined)
  if (typeof value === 'boolean') return value
  if (typeof value === 'string' && ['true', 'false'].includes(value.trim().toLowerCase())) return value.trim().toLowerCase() === 'true'
  return null
}

function taskName(task: ApiTask, index: number): string {
  const path = taskField(task, '_config_path')
  const fromPath = path.match(/\/tasks\/([^/]+)\/config\.yaml$/)?.[1]
  return taskField(task, 'name', 'title', 'task_id', 'id') || fromPath || `任务 ${index + 1}`
}

function taskMeta(task: ApiTask): { schedule: string; trigger: string; description: string } {
  const schedule = task.schedule
  const scheduleObj = schedule && typeof schedule === 'object' ? schedule as ApiTask : {}
  const trigger = task.trigger
  const triggerObj = trigger && typeof trigger === 'object' ? trigger as ApiTask : {}
  return {
    schedule: Object.keys(scheduleObj).length ? scheduleDescription(scheduleObj) : taskField(task, 'cron', 'frequency') || (typeof schedule === 'string' ? schedule : '未设置'),
    trigger: taskField(task, 'trigger_type') || taskField(triggerObj, 'type') || (typeof trigger === 'string' ? trigger : '未设置'),
    description: taskField(task, 'description', 'summary') || taskField(task, 'prompt', 'report_prompt') || '',
  }
}

function ReviewNotes({ review }: { review: DraftReview }) {
  return <section className="review-notes" aria-label="生成配置审核提示">
    <p><strong>原始需求</strong>：{review.original_request}</p>
    {!!review.conversation?.length && <details className="conversation-context"><summary>带入的业务讨论（{review.conversation.length} 条）</summary>{review.conversation.map((turn,index) => <p key={index}><strong>{turn.role === 'user' ? '你' : 'Genie'}：</strong>{turn.content}</p>)}</details>}
    {([
      ['生成时的假设（请核对）', review.assumptions],
      ['注意事项', review.warnings],
      ['生成时待补充的信息（编辑后仍需人工核对）', review.missing_information],
    ] as const).map(([title, items]) => items.length > 0 && <div key={title}><strong>{title}</strong><ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul></div>)}
  </section>
}

const initialMessage: ChatMessage = {
  id: 'welcome', role: 'assistant',
  source: 'welcome',
  text: '你好！你可以提出业务分析问题，也可以描述需要设置的自动任务。任务配置会先交给你审核，确认保存后才会进入“我的任务”。',
  agent: 'genie_one',
}

function App({ userLabel, authMode }: { userLabel?: string; authMode?: string }) {
  const [page, setPage] = useState<Page>('chat')
  const [reportTaskId, setReportTaskId] = useState('')
  const [trialSettings, setTrialSettings] = useState<TrialSettings>({ enabled: false })
  const [agent, setAgent] = useState<Agent>('genie_one')
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<Record<Agent, ChatMessage[]>>({ genie_one: [initialMessage] })
  const [sending, setSending] = useState(false)
  const [sendingStage, setSendingStage] = useState('正在理解你的需求…')
  const [workflow, setWorkflow] = useState({ enabled: false, analysis_provider: 'genie_one', router_model: '', metrics_enabled: false })
  const [traces, setTraces] = useState<WorkflowTrace[]>([])
  const [selectedTrace, setSelectedTrace] = useState<string>()
  const [chatError, setChatError] = useState<string | null>(null)
  const [tasks, setTasks] = useState<ApiTask[]>([])
  const [tasksLoading, setTasksLoading] = useState(false)
  const [tasksError, setTasksError] = useState<string | null>(null)
  const [tasksFetched, setTasksFetched] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [agentPickerOpen, setAgentPickerOpen] = useState(false)
  const [showTaskForm, setShowTaskForm] = useState(false)
  const [editingVersion, setEditingVersion] = useState<string | null>(null)
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null)
  const [showPreview, setShowPreview] = useState(false)
  const [taskForm, setTaskForm] = useState<TaskFormState>(EMPTY_FORM)
  const [taskSaving, setTaskSaving] = useState(false)
  const [requirements, setRequirements] = useState('')
  const [taskGenerating, setTaskGenerating] = useState(false)
  const [taskSQLGenerating, setTaskSQLGenerating] = useState(false)
  const [proposalOrigin, setProposalOrigin] = useState<ProposalOrigin | null>(null)
  const [conversationIds, setConversationIds] = useState<Partial<Record<Agent,string>>>({})
  const [workflowConversationIds, setWorkflowConversationIds] = useState<Record<string,string>>({})
  const generationRef = useRef(0)
  const generatingRef = useRef(false)
  const sqlRequestsRef = useRef(new Set<string>())
  const [taskError, setTaskError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ApiTask | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)

  const activeMessages = messages[agent]
  const counts = taskCounts(tasks)

  useEffect(() => {
    let active = true
    void apiRequest<TrialSettings>('/api/local-trials/settings')
      .then(value => { if (active) setTrialSettings({ ...value, enabled: value.enabled === true }) })
      .catch(() => { /* Trial routes exist only in an explicitly enabled local launcher. */ })
    void apiRequest<{ enabled?: boolean; analysis_provider?: string; router_model?: string; metrics_enabled?: boolean }>('/api/conversation/settings')
      .then(value => { if (active) setWorkflow({ enabled: value.enabled === true, analysis_provider: value.analysis_provider || 'genie_one', router_model: value.router_model || '', metrics_enabled: value.metrics_enabled === true }) })
      .catch(() => { /* Older servers keep the existing explicit task entry point. */ })
    return () => { active = false }
  }, [])

  function updateTrace(id: string | undefined, changes: Partial<WorkflowTrace>) {
    if (id) setTraces(previous => previous.map(trace => trace.id === id ? { ...trace, ...changes } : trace))
  }

  function proposalTraceId() {
    return proposalOrigin ? messages[proposalOrigin.agent].find(message => message.id === proposalOrigin.messageId)?.task?.traceId : undefined
  }

  function conditionRequest<T>(url: string, options?: RequestInit, timeoutMs?: number): Promise<T> {
    return apiRequest<T>(url, { ...options, headers: { ...options?.headers, ...traceHeaders(proposalTraceId()) } }, timeoutMs)
  }

  useEffect(() => {
    if (page === 'tasks' && !tasksFetched && !tasksLoading) void fetchTasks()
    // Intentionally load task data once on first visit; manual refresh is available.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, tasksFetched])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, sending, agent, page])

  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(event.target as Node)) setAgentPickerOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [])

  async function fetchTasks() {
    setTasksLoading(true)
    setTasksError(null)
    try {
      const data = await apiRequest<TasksResponse>('/api/tasks')
      if (!Array.isArray(data.tasks)) throw new Error('服务器返回的任务数据格式不正确。')
      setTasks(data.tasks)
      setTasksFetched(true)
    } catch (error) {
      setTasksError(error instanceof Error ? error.message : '获取任务失败。')
    } finally {
      setTasksLoading(false)
    }
  }

  function openTaskForm() {
    generationRef.current++
    generatingRef.current = false
    setTaskGenerating(false)
    setTaskSQLGenerating(false)
    setProposalOrigin(null)
    setRequirements('')
    setEditingTaskId(null)
    setEditingVersion(null)
    setTaskForm({ ...EMPTY_FORM, task_id: 'task_' + crypto.randomUUID().replaceAll('-', '') })
    setTaskError(null)
    setShowTaskForm(true)
    setShowPreview(false)
  }

  function editDraft(task: ApiTask) {
    openTaskForm()
    const defaults = buildCreatePayload(EMPTY_FORM)
    const config = {
      ...defaults, ...task,
      schedule: { ...defaults.schedule, ...(task.schedule as object) },
      trigger: { ...defaults.trigger, ...(task.trigger as object) },
      report: { ...defaults.report, ...(task.report as object) },
    } as DraftConfiguration
    setEditingTaskId(config.task_id)
    setEditingVersion(String(task.updated_at || ''))
    setTaskForm(proposalToForm(config))
  }

  function closeTaskForm() {
    generationRef.current++
    generatingRef.current = false
    setTaskGenerating(false)
    setTaskSQLGenerating(false)
    setShowTaskForm(false)
    setShowPreview(false)
    setProposalOrigin(null)
  }

  async function requestTriggerSQL(form: TaskFormState, traceId?: string) {
    const started = performance.now()
    updateTrace(traceId, { sql: 'running', sqlError: undefined })
    try {
      const data = await apiRequest<SQLSuggestion>('/api/conditions/sql/generate', {
        headers: traceHeaders(traceId), method: 'POST', body: JSON.stringify(sqlGenerationRequest(form)),
      }, 270000)
      updateTrace(traceId, { sql: 'done', sqlMs: performance.now() - started })
      return applySQLSuggestion(form, data)
    } catch (error) {
      updateTrace(traceId, { sql: 'error', sqlMs: performance.now() - started,
        sqlError: error instanceof Error ? error.message : 'SQL 生成失败，可单独重试。' })
      throw error
    }
  }

  async function generateCardSQL(seed: TaskSeed, messageId: string) {
    if (!seed.configuration || seed.configuration.trigger.type !== 'sql' || seed.configuration.trigger.sql.trim()
        || seed.saved || sqlRequestsRef.current.has(messageId)) return
    sqlRequestsRef.current.add(messageId)
    const patch = (changes: Partial<TaskSeed>) => setMessages(previous => ({ ...previous,
      [seed.agent]: previous[seed.agent].map(message => message.id === messageId
        && message.task && message.task.configuration === seed.configuration && !message.task.saved
        ? { ...message, task: { ...message.task, ...changes } } : message) }))
    patch({ sqlState: 'running', sqlError: undefined })
    try {
      const form = await requestTriggerSQL(proposalToForm(seed.configuration), seed.traceId)
      patch({ configuration: buildCreatePayload(form), sqlState: 'done' })
    } catch (error) {
      patch({ sqlState: 'error', sqlError: error instanceof Error ? error.message : 'SQL 生成失败；配置已保留，可单独重试。' })
    } finally {
      sqlRequestsRef.current.delete(messageId)
    }
  }

  async function generateTask(seed?: TaskSeed, messageId?: string) {
    const input = seed?.requirements || requirements
    if (!input.trim() || generatingRef.current) return
    const generation = ++generationRef.current
    generatingRef.current = true
    setTaskGenerating(true)
    setTaskError(null)
    if (seed) setChatError(null)
    updateTrace(seed?.traceId, { proposal: 'running', error: undefined })
    const apply = (form: TaskFormState) => {
      if (seed && messageId) {
        setMessages(previous => ({ ...previous, [seed.agent]: previous[seed.agent].map(message => message.id === messageId
          ? { ...message, task: { ...seed, configuration: buildCreatePayload(form), sqlError: undefined, sqlState: undefined } } : message) }))
      } else setTaskForm(form)
    }
    let configurationReady = false
    try {
      const data = await apiRequest<{ configuration: DraftConfiguration }>('/api/tasks/generate', {
        headers: traceHeaders(seed?.traceId || proposalTraceId()),
        method: 'POST', body: JSON.stringify({ requirements: input.trim(),
          conversation: seed?.conversation || taskForm.draft_review?.conversation || [],
          source_agent: seed?.agent || taskForm.draft_review?.source_agent || null }),
      }, 75000)
      if (generation !== generationRef.current) return
      const form = proposalToForm({ ...data.configuration, task_id: seed ? data.configuration.task_id : editingTaskId || data.configuration.task_id })
      apply(form)
      setShowPreview(false)
      configurationReady = true
      updateTrace(seed?.traceId, { proposal: 'done', save: 'waiting', taskAgents: [form.report_agent, form.trigger_agent] })
      if (form.trigger_type === 'sql' && !form.trigger_sql.trim()) {
        setTaskSQLGenerating(true)
        const completed = await requestTriggerSQL(form, seed?.traceId || proposalTraceId())
        if (generation !== generationRef.current) return
        apply(completed)
      } else updateTrace(seed?.traceId, { sql: 'skipped', sqlError: undefined })
    } catch (error) {
      if (generation === generationRef.current) {
        const message = (configurationReady ? '任务配置已生成，但触发 SQL 尚未生成。可在编辑面板重试“用 Genie 生成 SQL”，不用重新解析任务。' : '') + (error instanceof Error ? error.message : '生成失败，请手动创建草稿。')
        updateTrace(seed?.traceId, { proposal: configurationReady ? 'done' : 'error', error: message })
        if (seed) setChatError(message)
        else setTaskError(message)
      }
    } finally {
      if (generation === generationRef.current) {
        generatingRef.current = false
        setTaskGenerating(false)
        setTaskSQLGenerating(false)
      }
    }
  }

  async function saveTask(formOverride?: TaskFormState, originOverride?: ProposalOrigin) {
    if (taskSaving) return
    const form = formOverride || taskForm
    const origin = originOverride || proposalOrigin
    const existingId = originOverride ? null : editingTaskId
    const existingVersion = originOverride ? null : editingVersion
    const traceId = origin ? messages[origin.agent].find(message => message.id === origin.messageId)?.task?.traceId : undefined
    const validation = formError(form)
    if (validation) {
      updateTrace(traceId, { save: 'error', error: validation })
      if (origin && !showTaskForm) setChatError(validation)
      else setTaskError(validation)
      return
    }
    setTaskSaving(true)
    updateTrace(traceId, { save: 'running', error: undefined })
    setTaskError(null)
    try {
      await apiRequest<{ status: string }>(existingId ? `/api/tasks/${encodeURIComponent(existingId)}` : '/api/tasks/create', {
        headers: traceHeaders(traceId),
        method: existingId ? 'PUT' : 'POST', body: JSON.stringify({ ...buildCreatePayload(form), ...(existingVersion ? { expected_updated_at: existingVersion } : {}) }),
      }, 60000)
      updateTrace(traceId, { save: 'done', taskAgents: [form.report_agent, form.trigger_agent] })
      setShowTaskForm(false)
      setShowPreview(false)
      if (origin) {
        setMessages(previous => ({ ...previous, [origin.agent]: previous[origin.agent].map(message => message.id === origin.messageId && message.task
          ? { ...message, text: `已保存任务草稿「${form.name}」。可在下方审核启用，并直接查看此任务的分析报告。启用不会立即运行或启动 Job。`, task: { ...message.task, configuration: buildCreatePayload(form), saved: true } } : message) }))
        setChatError(null)
      } else if (!existingId) {
        // Dedicated creation uses the same saved-task actions as conversational creation.
        setMessages(previous => ({ ...previous, [agent]: [...previous[agent], {
          id: crypto.randomUUID(), role: 'assistant', source: 'system',
          text: `已保存任务草稿「${form.name}」。可在下方审核启用，并查看此任务的分析报告。启用不会立即运行或启动 Job。`,
          task: { requirements: form.draft_review?.original_request || requirements || form.description || form.name,
            conversation: form.draft_review?.conversation || [], agent, configuration: buildCreatePayload(form), saved: true },
        }] }))
        setChatError(null)
      }
      setTaskForm(EMPTY_FORM)
      setProposalOrigin(null)
      setEditingTaskId(null)
      setEditingVersion(null)
      await fetchTasks()
      setPage(origin || !existingId ? 'chat' : 'tasks')
    } catch (error) {
      updateTrace(traceId, { save: 'error', error: error instanceof Error ? error.message : '保存未确认' })
      if (origin && !showTaskForm) setChatError(error instanceof Error ? error.message : '保存失败。')
      else setTaskError(error instanceof Error ? error.message : '保存失败。')
    } finally {
      setTaskSaving(false)
    }
  }

  async function deleteDraftTask(task: ApiTask) {
    const tid = taskField(task, 'task_id')
    if (!tid) return
    try {
      await apiRequest<{ status: string }>(`/api/tasks/${encodeURIComponent(tid)}`, {
        method: 'DELETE', body: JSON.stringify({ confirm: true }),
      }, 30000)
      setDeleteTarget(null)
      await fetchTasks()
    } catch (error) {
      setTaskError(error instanceof Error ? error.message : '删除失败。')
    }
  }

  function taskSource(task: ApiTask): string {
    const src = task._source
    return typeof src === 'string' ? src : 'yaml'
  }

  function editConversationProposal(message: ChatMessage) {
    if (!message.task?.configuration) return
    const task = message.task
    if (task.saved) {
      const stored = tasks.find(item => item.task_id === task.configuration!.task_id)
      if (!stored) {
        setChatError('任务详情尚未读取，请稍后再次点击“编辑草稿”。')
        void fetchTasks()
        return
      }
      editDraft(stored)
    } else {
      openTaskForm()
      setTaskForm(proposalToForm(task.configuration!))
    }
    setRequirements(task.requirements)
    setProposalOrigin({ agent: task.agent, messageId: message.id })
  }

  function offerTaskFromAnalysis(messageId: string) {
    const index = messages[agent].findIndex(message => message.id === messageId)
    if (index < 0) return
    const context = conversationContext(messages[agent].slice(0,index+1))
    setMessages(previous => ({ ...previous, [agent]: [...previous[agent], {
      id: crypto.randomUUID(), role: 'assistant', source: 'system',
      text: '已带入这段分析。请补充你希望的检查频率和触发条件，再生成草稿。',
      task: { requirements: '把这段分析设为自动任务；未明确的频率和条件请列为待补充。', conversation: context, agent },
    }] }))
  }

  function offerTaskFromComposer() {
    const context = conversationContext(messages[agent])
    const input = draft.trim() || '把这段讨论设为自动任务；未明确的频率和条件请列为待补充。'
    setMessages(previous => ({ ...previous, [agent]: [...previous[agent], {
      id: crypto.randomUUID(), role: 'assistant', source: 'system',
      text: '已准备任务草稿入口。请核对带入的上下文并补充要求，再点击生成。',
      task: { requirements: input, conversation: context, agent },
    }] }))
    setDraft('')
  }

  function taskStatus(task: ApiTask): string {
    const status = task.lifecycle_status
    return typeof status === 'string' ? status : taskSource(task) === 'registry' ? 'draft' : 'active'
  }

  async function ask(question: string) {
    const text = question.trim()
    if (!text || sending) return
    const selectedAgent = agent
    if (workflow.enabled) {
      await askWorkflow(text, selectedAgent)
      return
    }
    if (isTaskCreationIntent(text)) {
      const context = conversationContext(messages[selectedAgent])
      setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent],
        { id: crypto.randomUUID(), role: 'user', text },
        { id: crypto.randomUUID(), role: 'assistant', source: 'system',
          text: '可以把这段讨论设为任务。已带入相关对话；点击生成后审核配置，明确保存才会进入“我的任务”。',
          task: { requirements: text, conversation: context, agent: selectedAgent } },
      ] }))
      setDraft('')
      return
    }
    setChatError(null)
    setDraft('')
    setMessages(previous => ({
      ...previous,
      [selectedAgent]: [...previous[selectedAgent], { id: crypto.randomUUID(), role: 'user', text }],
    }))
    setSending(true)
    setSendingStage('正在分析数据，这可能需要一些时间…')
    try {
      const data = await apiRequest<ChatResponse>('/api/chat', {
        method: 'POST', body: JSON.stringify({ question: text, agent: selectedAgent, conversation_id: conversationIds[selectedAgent] || null }),
      }, API_TIMEOUT_MS)
      if (typeof data.answer !== 'string' || !data.answer.trim()) {
        throw new Error('Genie 未返回有效的分析内容。')
      }
      if (data.conversation_id) setConversationIds(previous => ({ ...previous, [selectedAgent]: data.conversation_id }))
      setMessages(previous => ({
        ...previous,
        [selectedAgent]: [...previous[selectedAgent], { id: crypto.randomUUID(), role: 'assistant', text: data.answer!, agent: selectedAgent }],
      }))
    } catch (error) {
      setChatError(error instanceof Error ? error.message : '请求失败。')
      setDraft(text)
    } finally {
      setSending(false)
    }
  }

  async function askWorkflow(text: string, selectedAgent: Agent) {
    const traceId = crypto.randomUUID()
    setTraces(previous => [...previous, { id: traceId, request: text, routing: 'running',
      analysis: 'waiting', proposal: 'waiting', save: 'waiting', provider: workflow.analysis_provider }])
    setSelectedTrace(traceId)
    const context = conversationContext(messages[selectedAgent])
    setChatError(null)
    setDraft('')
    setSending(true)
    setSendingStage('正在理解需求并整理任务配置…')
    setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent], { id: crypto.randomUUID(), role: 'user', text, traceId }] }))
    let routed = false
    let deferred: { id: string; seed: TaskSeed } | undefined
    let sqlPending: Promise<void> | undefined
    try {
      const plan = await apiRequest<WorkflowPlan>('/api/conversation/route', {
        headers: traceHeaders(traceId),
        method: 'POST', body: JSON.stringify({ requirements: text, conversation: context, source_agent: selectedAgent }),
      }, 75000)
      routed = true
      const hasTask = plan.intent === 'task' || plan.intent === 'mixed'
      updateTrace(traceId, { routing: 'done', intent: plan.intent, order: plan.execution_order,
        analysis: plan.analysis_ticket ? 'waiting' : 'skipped', proposal: hasTask ? plan.configuration ? 'done' : 'waiting' : 'skipped',
        save: hasTask ? 'waiting' : 'skipped', analysisAgent: plan.analysis_agent, analysisQuestion: plan.analysis_question,
        sql: hasTask ? plan.configuration?.trigger.type === 'sql' && !plan.configuration.trigger.sql.trim() ? 'waiting' : plan.configuration ? 'skipped' : 'waiting' : 'skipped',
        taskRequirements: plan.task_requirements, routingMs: plan.metrics?.routing_ms, tokens: plan.metrics?.usage?.total_tokens,
        taskAgents: plan.configuration ? [plan.configuration.report.agent, ...(plan.configuration.trigger.context_agents || [])] : undefined })
      if (plan.intent === 'clarify') {
        setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent], {
          id: crypto.randomUUID(), role: 'assistant', source: 'system', text: plan.clarification,
        }] }))
        return
      }
      if (plan.intent === 'task' || plan.intent === 'mixed') {
        const id = crypto.randomUUID()
        const seed: TaskSeed = { requirements: plan.task_requirements || text, conversation: context,
          agent: selectedAgent, configuration: plan.configuration || undefined, traceId }
        if (plan.execution_order === 'analysis_first') deferred = { id, seed }
        else {
          setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent], {
            id, role: 'assistant', source: 'system',
            text: '已整理任务配置；量化条件将继续用 Genie 生成 SQL。请审核条件后明确保存；尚未保存或启用任务。', task: seed,
          }] }))
          // Independent analysis and SQL generation proceed in parallel, with separate errors.
          sqlPending = generateCardSQL(seed, id)
        }
      }
      if (plan.analysis_ticket) {
        updateTrace(traceId, { analysis: 'running' })
        const namespace = 'genie_one'
        setSendingStage(workflow.analysis_provider === 'genie_one' ? 'Genie One 正在分析业务数据…' : 'Genie 正在分析业务数据…')
        const data = await apiRequest<ChatResponse>('/api/conversation/analysis', {
          headers: traceHeaders(traceId),
          method: 'POST', body: JSON.stringify({ ticket: plan.analysis_ticket, conversation_id: workflowConversationIds[namespace] || null }),
        }, API_TIMEOUT_MS)
        if (!data.answer?.trim()) throw new Error('分析接口未返回有效内容。')
        updateTrace(traceId, { analysis: 'done', analysisMs: data.analysis_ms,
          provider: data.provider === 'genie_one_mcp' ? 'genie_one' : data.provider || workflow.analysis_provider })
        if (data.conversation_id) setWorkflowConversationIds(previous => ({ ...previous, [namespace]: data.conversation_id! }))
        const answer: ChatMessage = { id: crypto.randomUUID(), role: 'assistant', text: data.answer,
          agent: selectedAgent, provider: data.provider }
        setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent], answer] }))
        if (deferred) {
          const followup = deferred
          const history = conversationContext([...messages[selectedAgent], { role: 'user', text }, answer])
          setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent], {
            id: followup.id, role: 'assistant', source: 'system',
            text: '分析已完成。任务条件依赖本次结果，请核对后点击生成配置；系统不会自行决定阈值或启用任务。',
            task: { ...followup.seed, conversation: history },
          }] }))
          deferred = undefined
        }
      }
    } catch (error) {
      updateTrace(traceId, { ...(routed ? { analysis: 'error' as const } : { routing: 'error' as const }),
        error: error instanceof Error ? error.message : '处理失败' })
      setChatError(error instanceof Error ? error.message : '对话处理失败。')
      if (!routed) setDraft(text)
      if (deferred) {
        const followup = deferred
        setMessages(previous => ({ ...previous, [selectedAgent]: [...previous[selectedAgent], {
          id: followup.id, role: 'assistant', source: 'system',
          text: '分析未完成，尚不能确定依赖分析的任务条件。已保留任务要求，可补充信息后手动生成草稿。', task: followup.seed,
        }] }))
      }
    } finally {
      await sqlPending
      setSending(false)
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void ask(draft)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void ask(draft)
    }
  }

  function navigate(next: Page, taskId = '') {
    if (next === 'reports') setReportTaskId(taskId)
    setPage(next)
    setSidebarOpen(false)
    setChatError(null)
  }

  function chooseAgent(next: Agent) {
    if (sending) return
    setAgent(next)
    setAgentPickerOpen(false)
    setChatError(null)
    setDraft('')
  }

  return (
    <div className="app-shell">
      {sidebarOpen && <button className="mobile-overlay" onClick={() => setSidebarOpen(false)} aria-label="关闭菜单" />}
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="brand"><span className="brand-mark"><Sparkles size={20} strokeWidth={2.3}/></span><div><strong>Business AI Hub</strong><span>智能业务工作台</span></div></div>
        <div className="nav-label">工作空间</div>
        <nav className="main-nav" aria-label="主导航">
          <button className={page === 'chat' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('chat')}><MessageSquareText size={18}/><span>AI 分析</span>{page === 'chat' && <span className="nav-dot" />}</button>
          <button className={page === 'tasks' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('tasks')}><ClipboardList size={18}/><span>我的任务</span>{page === 'tasks' && <span className="nav-dot" />}</button>
          <button className={page === 'reports' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('reports')}><FileText size={18}/><span>分析报告</span>{page === 'reports' && <span className="nav-dot" />}</button>
        </nav>
        <div className="sidebar-bottom">
          <div className="workspace-card"><div className="workspace-card-icon"><ShieldCheck size={17}/></div><div><strong>Databricks Workspace</strong><p>个人身份 · 按权限访问</p></div></div>
          <div className="sidebar-footer"><span className="online-indicator" />服务已连接 <span className="version">v0.4</span></div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="打开菜单"><Menu size={21}/></button>
          <div className="breadcrumbs"><span>工作空间</span><span className="crumb-separator">/</span><strong>{page === 'chat' ? 'AI 分析' : page === 'tasks' ? '我的任务' : '分析报告'}</strong></div>
          <div className="topbar-right"><span className="environment-pill"><span className="online-indicator"/> {authMode === 'local_cli' ? '本地用户测试' : '个人权限已连接'}</span><span title={userLabel}>{userLabel}</span><span className="avatar">AI</span></div>
        </header>

        {page === 'chat' && <section className="chat-page" aria-hidden={showTaskForm || undefined}>
          <div className="page-heading chat-heading"><div><div className="eyebrow"><Sparkles size={14}/> BUSINESS INTELLIGENCE</div><h1>与数据对话</h1><p>用自然语言提问，让业务数据给出清晰的答案。</p></div></div>
          <div className="agent-row"><div className="agent-selector" ref={pickerRef}>
            <button className="agent-trigger" onClick={() => setAgentPickerOpen(!agentPickerOpen)} aria-expanded={agentPickerOpen} aria-haspopup="listbox" disabled={sending || workflow.analysis_provider === 'genie_one'}>
              <span className={`agent-icon ${agent === 'genie_one' ? 'sales' : 'market'}`}>{agent === 'genie_one' ? <BarChart3 size={20}/> : <Target size={20}/>}</span>
              <span className="agent-trigger-copy"><span className="agent-overline">当前分析助手</span><strong>{workflow.analysis_provider === 'genie_one' ? 'Genie One' : AGENTS[agent].name}</strong></span><ChevronDown size={17} className="chevron"/>
            </button>
            {agentPickerOpen && <div className="agent-menu" role="listbox" aria-label="选择分析助手">{(Object.keys(AGENTS) as Agent[]).map(key => <button key={key} role="option" aria-selected={key === agent} className="agent-option" onClick={() => chooseAgent(key)}><span className={`agent-icon ${key === 'genie_one' ? 'sales' : 'market'}`}>{key === 'genie_one' ? <BarChart3 size={18}/> : <Target size={18}/>}</span><span><strong>{AGENTS[key].name}</strong><small>{AGENTS[key].subtitle}</small></span>{key === agent && <Check size={17} className="option-check"/>}</button>)}</div>}
          </div><span className="powered-by"><span className="online-indicator"/> {workflow.enabled ? '统一对话 · 分析与任务草稿' : 'Connected to Databricks Genie'}</span></div>
          <button className="secondary-button" disabled={sending || taskGenerating || taskSaving || activeMessages.length === 0} onClick={() => { setMessages(previous => ({ ...previous, [agent]: [] })); setConversationIds(previous => ({ ...previous, [agent]: undefined })); setWorkflowConversationIds({}); setTraces([]); setSelectedTrace(undefined); setChatError(null) }}>新对话</button>

          <div className={workflow.enabled ? 'conversation-workspace' : undefined}>
          <div className="chat-panel">
            <div className="conversation" role="log" aria-label="对话记录" aria-live="polite">
              {activeMessages.length === 0 && <div className="blank-conversation"><span className="empty-orb"><Sparkles size={28}/></span><h2>开始探索市场数据</h2><p>选择下面的问题，或者提出你自己的业务问题。</p></div>}
              {activeMessages.map(message => <div key={message.id} className={`chat-message ${message.role}`}>
                <div className={`message-avatar ${message.role === 'assistant' ? 'assistant-avatar' : 'user-avatar'}`}>{message.role === 'assistant' ? <Sparkles size={17}/> : '你'}</div>
                <div className="message-content"><span className="speaker-name">{message.role === 'assistant' ? message.source === 'system' ? '系统 · 任务配置引导' : message.source === 'welcome' ? '业务助手' : message.provider === 'genie_one_mcp' ? 'Genie One' : AGENTS[message.agent || agent].name : '你'}</span><div className={`message-bubble ${message.role}`}>
                  {message.role === 'assistant' ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text}</ReactMarkdown> : <p>{message.text}</p>}
                  {message.task && <TaskProposalCard task={message.task} busy={taskGenerating || message.task.sqlState === 'running'} saving={taskSaving} sqlBusy={taskSQLGenerating || message.task.sqlState === 'running'} analysisBusy={sending}
                    onRequirements={text => { updateTrace(message.task?.traceId, { proposal: 'waiting', sql: 'waiting', sqlError: undefined, save: 'waiting', taskRequirements: text, error: undefined }); setMessages(previous => ({ ...previous, [agent]: previous[agent].map(item => item.id === message.id && item.task ? { ...item, task: { ...item.task, requirements: text, configuration: undefined, sqlState: undefined, sqlError: undefined } } : item) })) }}
                    onGenerate={() => void generateTask(message.task,message.id)} onEdit={() => editConversationProposal(message)}
                    onGenerateSQL={() => message.task && void generateCardSQL(message.task,message.id)}
                    onAnalyzeRule={() => { if (!sending && message.task) void askWorkflow(('请先分析历史数据，提出可供我确认的异常规则或范围，再根据我确认的规则设置任务；不要自行决定阈值。原始任务需求：' + message.task.requirements).slice(0,4000),message.task.agent) }}
                    onSave={() => message.task?.configuration && void saveTask(proposalToForm(message.task.configuration),{ agent: message.task.agent, messageId: message.id })}
                    savedActions={message.task.saved && message.task.configuration && <>
                      <span className="status-chip">{({ active: '已启用', paused: '已暂停', draft: '草稿' } as Record<string,string>)[String(tasks.find(item => item.task_id === message.task!.configuration!.task_id)?.lifecycle_status || 'draft')] || '状态待刷新'}</span>
                      <TaskLifecycle task={tasks.find(item => item.task_id === message.task!.configuration!.task_id) || { ...message.task.configuration, lifecycle_status: 'draft' }}
                        request={(url, options, timeout) => apiRequest(url, { ...options, headers: { ...options?.headers, ...traceHeaders(message.task?.traceId) } }, timeout)}
                        onChanged={fetchTasks} onEdit={() => editConversationProposal(message)}/>
                      <TaskTrial task={tasks.find(item => item.task_id === message.task!.configuration!.task_id) || message.task.configuration}
                        settings={trialSettings} request={apiRequest} onViewReports={() => navigate('reports',message.task?.configuration?.task_id)}/>
                      <button className="secondary-button" onClick={() => navigate('reports',message.task?.configuration?.task_id)}>查看此任务的分析报告</button>
                    </>}
                    onViewTasks={() => navigate('tasks')}/>}
                </div>
                {message.role === 'assistant' && !message.source && <button className="secondary-button" disabled={sending || taskGenerating} onClick={() => offerTaskFromAnalysis(message.id)}>设为自动任务</button>}
                {message.traceId && <button className="flow-message-link" onClick={() => setSelectedTrace(message.traceId)}>查看这条消息的分流流程</button>}
                </div>
              </div>)}
              {sending && <div className="chat-message assistant"><div className="message-avatar assistant-avatar"><Sparkles size={17}/></div><div className="message-content"><span className="speaker-name">业务助手</span><div className="thinking"><span className="loading-dots"><i/><i/><i/></span><span>{sendingStage}</span></div></div></div>}
              <div ref={bottomRef}/>
            </div>
            {activeMessages.length < 3 && !sending && <div className="suggestions"><div className="suggestions-label"><Sparkles size={14}/> 试试这样提问</div><div className="suggestions-grid">{SUGGESTIONS[agent].map((suggestion, i) => <button className="suggestion" key={i} onClick={() => void ask(suggestion)}><span>{suggestion}</span><ArrowRight size={16}/></button>)}</div></div>}
            {chatError && <div className="error-banner" role="alert"><span>{chatError}</span><button onClick={() => setChatError(null)} aria-label="关闭错误"><X size={15}/></button></div>}
            <form className="composer" onSubmit={onSubmit}><div className="composer-field"><textarea aria-label="输入业务问题" placeholder="向业务数据提问…" value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={onKeyDown} rows={2} disabled={sending}/><button type="submit" className="send-button" disabled={!draft.trim() || sending} title="发送消息"><Send size={18}/></button></div><div className="composer-bottom"><button type="button" className="task-handoff" disabled={sending} onClick={offerTaskFromComposer}>设置自动任务</button><span>Enter 发送 · Shift + Enter 换行</span><span>对话仅保留在本页；任务保存后持久化</span></div></form>
          </div>
          {workflow.enabled && <WorkflowInspector traces={traces} selected={selectedTrace} onSelect={setSelectedTrace}
            provider={workflow.analysis_provider} model={workflow.router_model} metricsEnabled={workflow.metrics_enabled}/>}
          </div>
        </section>}

        {page === 'tasks' && <section className="content-page"><div className="page-heading"><div><div className="eyebrow"><Activity size={14}/> TASK AUTOMATION</div><h1>我的任务</h1><p>管理任务配置，创建草稿并预览。</p></div><div className="heading-actions"><button className="primary-button" onClick={openTaskForm}><Plus size={16}/>创建任务</button><button className="secondary-button" onClick={() => void fetchTasks()} disabled={tasksLoading}><RefreshCcw size={16} className={tasksLoading ? 'spin' : ''}/>刷新</button></div></div>
          <div className="metric-grid"><div className="metric-card"><span>任务总数</span><div><strong>{tasksFetched ? tasks.length : '—'}</strong><span className="metric-icon grey"><ClipboardList size={20}/></span></div><p>已配置的自动化任务</p></div><div className="metric-card"><span>已启用</span><div><strong>{tasksFetched ? counts.enabled : '—'}</strong><span className="metric-icon green"><Check size={20}/></span></div><p>配置已启用，不代表 Job 正在运行</p></div><div className="metric-card"><span>已停用</span><div><strong>{tasksFetched ? counts.disabled : '—'}</strong><span className="metric-icon amber"><Clock3 size={20}/></span></div><p>不含草稿</p></div><div className="metric-card"><span>草稿</span><div><strong>{tasksFetched ? counts.drafts : '—'}</strong><span className="metric-icon amber"><FileText size={20}/></span></div><p>审核启用后才可执行</p></div></div>
          <div className="section-title"><div><h2>任务列表</h2><span className="count-pill">{tasksFetched ? tasks.length : '—'}</span></div><span className="section-subtitle">{trialSettings.enabled ? '来源：本机 SQLite 任务 + 导出的 YAML 配置' : '来源：Databricks Workspace · 实时配置'}</span></div>
          {tasksLoading && <div className="content-state"><span className="spinner"/>正在加载任务…</div>}
          {tasksError && <div className="content-state error-state"><p>无法读取任务列表</p><small>{tasksError}</small><button className="secondary-button" onClick={() => void fetchTasks()}><RefreshCcw size={15}/>重试</button></div>}
          {!tasksLoading && !tasksError && tasksFetched && tasks.length === 0 && <div className="content-state"><ClipboardList size={26}/><p>暂无可显示的任务</p><small>请确认 Task Manager 配置目录中存在任务。</small></div>}
          {!tasksLoading && !tasksError && tasksFetched && tasks.length > 0 && <div className="task-list">{tasks.map((task, index) => {
            const meta = taskMeta(task)
            const enabled = taskEnabled(task)
            const source = taskSource(task)
            const status = taskStatus(task)
            const isDraft = status === 'draft'
            const report = task.report && typeof task.report === 'object' ? task.report as ApiTask : {}
            const review = report.draft_review as DraftReview | null | undefined
            return <div className="task-row" key={taskField(task, '_config_path', 'id', 'task_id') || index}><div className="task-identity"><div className="task-icon"><CalendarDays size={20}/></div><div><h3>{taskName(task, index).replaceAll('_', ' ')}</h3><p>{meta.description || `触发方式：${meta.trigger}`}</p>{review && <details className="saved-review"><summary>查看需求与生成说明</summary><ReviewNotes review={review}/></details>}</div></div><div className="task-details"><span><Clock3 size={15}/> {meta.schedule}</span><span><Activity size={15}/> {meta.trigger}</span><span className={`source-badge ${source}`}>{source === 'yaml' ? 'YAML' : 'Registry'}</span></div><div className="task-actions"><span className={`status-chip ${isDraft ? 'draft' : enabled === true ? 'enabled' : enabled === false ? 'disabled' : 'unknown'}`}><i/>{isDraft ? '草稿' : enabled === true ? '已启用' : enabled === false ? '已停用' : '状态未知'}</span>{source === 'registry' && <><TaskLifecycle task={task} request={apiRequest} onChanged={fetchTasks} onEdit={() => editDraft(task)}/><TaskTrial task={task} settings={trialSettings} request={apiRequest} onViewReports={() => navigate('reports',String(task.task_id))}/></>}{isDraft && <button className="icon-button delete" onClick={() => setDeleteTarget(task)} title="删除草稿"><X size={15}/></button>}</div></div>
          })}</div>}
          <div className="info-note"><ShieldCheck size={17}/><span>YAML 任务通过原配置管理。Registry 草稿须审核启用；暂停可阻止后续批次。现有 Job 未恢复，启用配置不会启动 Job。</span></div>
        </section>}

        {showTaskForm && <div className="modal-overlay" onClick={() => { if (!taskSaving) closeTaskForm() }}><div className="modal-content" role="dialog" aria-modal="true" aria-label="创建任务草稿" onClick={e => e.stopPropagation()}><div className="modal-header"><h2>{showPreview ? '预览任务配置' : editingTaskId ? '编辑任务草稿' : '创建任务'}</h2><button className="icon-button" aria-label="关闭创建任务" disabled={taskSaving} onClick={() => { if (!taskSaving) closeTaskForm() }}><X size={18}/></button></div>

          {taskError && <div className="error-banner"><span>{taskError}</span><button onClick={() => setTaskError(null)}><X size={15}/></button></div>}

          {!showPreview && <section className="generate-panel" aria-label="自然语言创建任务">
            <label htmlFor="task-requirements">用自然语言描述任务</label>
            <textarea id="task-requirements" value={requirements} onChange={e => setRequirements(e.target.value)} maxLength={4000} rows={3} disabled={taskGenerating || taskSaving} placeholder="例如：每周一检查善存上一周的 GMV，如果比前一周下降超过 15%，分析原因并生成销售风险报告。" />
            <div className="generate-actions"><small>{taskForm.draft_review ? '重新生成会替换下方编辑内容；确认后才会保存草稿。' : '生成只填充下方表单；审核确认后才会保存草稿。'}</small><button className="primary-button" onClick={() => void generateTask()} disabled={!requirements.trim() || taskGenerating || taskSaving}><Sparkles size={15}/>{taskGenerating ? (taskSQLGenerating ? '正在生成 SQL…' : '正在生成…') : taskForm.draft_review ? '重新生成配置' : '生成任务配置'}</button></div>
            <p className="form-hint">条件任务会继续调用所需 Genie 生成 SQL（会查询数据；单域 1 个、跨域最多 2 个 Genie 会话），供你审核；不会自动运行条件测试。也可手动填写。</p>
          </section>}

          {taskForm.draft_review && <ReviewNotes review={taskForm.draft_review}/>}

          {!showPreview && <fieldset className="task-form" disabled={taskGenerating || taskSaving}>
            <legend className="form-legend">审核并编辑任务配置</legend>
            <div className="form-field"><label htmlFor="task-id">任务 ID</label><input id="task-id" value={taskForm.task_id} onChange={e => setTaskForm(f => ({ ...f, task_id: e.target.value }))} placeholder="unique_task_id" /></div>
            <div className="form-field"><label htmlFor="task-name">任务名称 *</label><input id="task-name" value={taskForm.name} onChange={e => setTaskForm(f => ({ ...f, name: e.target.value }))} placeholder="任务名称" /></div>
            <div className="form-field"><label htmlFor="task-description">描述</label><textarea id="task-description" value={taskForm.description} onChange={e => setTaskForm(f => ({ ...f, description: e.target.value }))} rows={2} placeholder="任务描述" /></div>
            <div className="form-row"><div className="form-field"><label htmlFor="schedule-frequency">调度频率</label><select id="schedule-frequency" value={taskForm.schedule_freq} onChange={e => setTaskForm(f => ({ ...f, schedule_freq: e.target.value as ScheduleFreq }))}><option value="daily">每日</option><option value="weekly">每周</option><option value="interval">间隔天数</option></select></div>
              {taskForm.schedule_freq === 'weekly' && <div className="form-field"><label htmlFor="schedule-weekday">星期</label><select id="schedule-weekday" value={taskForm.schedule_weekday} onChange={e => setTaskForm(f => ({ ...f, schedule_weekday: e.target.value }))}>{WEEKDAYS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>}
              {taskForm.schedule_freq === 'interval' && <div className="form-field"><label htmlFor="interval-days">间隔天数</label><input id="interval-days" type="number" min={1} max={365} value={Number.isFinite(taskForm.interval_days) ? taskForm.interval_days : ''} onChange={e => setTaskForm(f => ({ ...f, interval_days: e.target.valueAsNumber }))} /></div>}
            </div>
            <div className="form-row"><div className="form-field"><label htmlFor="schedule-time">检查时间</label><input id="schedule-time" type="time" value={taskForm.schedule_time} onChange={e => setTaskForm(f => ({ ...f, schedule_time: e.target.value }))}/></div><div className="form-field"><label htmlFor="schedule-timezone">时区</label><input id="schedule-timezone" list="timezones" value={taskForm.schedule_timezone} onChange={e => setTaskForm(f => ({ ...f, schedule_timezone: e.target.value }))}/><datalist id="timezones"><option value="Asia/Shanghai"/><option value="UTC"/><option value="America/Chicago"/></datalist></div></div>
            {taskForm.schedule_freq === 'interval' && <div className="form-field"><label htmlFor="schedule-start">开始日期（间隔基准）</label><input id="schedule-start" type="date" value={taskForm.schedule_start_date} onChange={e => setTaskForm(f => ({ ...f, schedule_start_date: e.target.value }))}/></div>}
            <p className="form-hint">{scheduleDescription(buildCreatePayload(taskForm).schedule)}。这是计划配置，保存后不会开启调度。</p>
            <div className="form-row"><div className="form-field"><label htmlFor="trigger-type">触发条件</label><select id="trigger-type" value={taskForm.trigger_type} onChange={e => setTaskForm(f => ({ ...f, trigger_type: e.target.value as TriggerType, agent_review: {} }))}><option value="always">每次检查都生成报告</option><option value="sql">SQL 条件</option><option value="agent">AI 判断条件（仅定性判断）</option><option value="unconfigured">待补充条件</option></select></div></div>
            {taskForm.trigger_type === 'sql' && <div className="form-field"><label htmlFor="trigger-sql">触发 SQL 条件（待审核）</label><textarea id="trigger-sql" value={taskForm.trigger_sql} onChange={e => setTaskForm(f => ({ ...f, trigger_sql: e.target.value, sql_review: {} }))} rows={3} placeholder="粘贴已核对的 SQL；不确定时可留空，保存待补充草稿。" /></div>}
            {taskForm.trigger_type !== 'always' && <div className="form-field"><label htmlFor="trigger-prompt">业务判断条件</label><textarea id="trigger-prompt" value={taskForm.trigger_prompt} onChange={e => setTaskForm(f => ({ ...f, trigger_prompt: e.target.value, sql_review: {}, agent_review: {} }))} rows={3} placeholder="保留指标口径、比较周期、阈值；不确定之处请注明。" /></div>}
            {taskForm.trigger_type !== 'always' && <ConditionSQLFields form={taskForm} setForm={setTaskForm} request={conditionRequest}/>}
            <div className="form-row"><div className="form-field"><label htmlFor="report-agent">报告 Agent</label><select id="report-agent" value={taskForm.report_agent} onChange={e => setTaskForm(f => ({ ...f, report_agent: e.target.value }))}><option value="genie_one">Genie One</option></select></div><div className="form-field"><label htmlFor="task-owner">负责人</label><input id="task-owner" value={userLabel || taskForm.owner} readOnly title="权限绑定登录账户，不能通过填写负责人更改" /></div></div>
            <div className="form-field"><label htmlFor="report-prompt">报告分析要求</label><textarea id="report-prompt" value={taskForm.report_prompt} onChange={e => setTaskForm(f => ({ ...f, report_prompt: e.target.value }))} rows={3} placeholder="报告生成时的提示词" /></div>
            <div className="form-field"><label htmlFor="report-template">报告模板（可选）</label><textarea id="report-template" value={taskForm.report_template} onChange={e => setTaskForm(f => ({ ...f, report_template: e.target.value }))} rows={2}/></div>
            <EmailFields form={taskForm} setForm={setTaskForm} request={apiRequest}/>
            <div className="form-actions"><button className="secondary-button" onClick={closeTaskForm}>取消</button><button className="primary-button" onClick={() => { const error = formError(taskForm); setTaskError(error); if (!error) setShowPreview(true) }}>预览并保存</button></div>
          </fieldset>}

          {showPreview && <div className="task-preview">
            <div className="preview-section"><span className="preview-label">任务 ID</span><span className="preview-value">{taskForm.task_id}</span></div>
            <div className="preview-section"><span className="preview-label">名称</span><span className="preview-value">{taskForm.name}</span></div>
            <div className="preview-section"><span className="preview-label">描述</span><span className="preview-value">{taskForm.description || '—'}</span></div>
            <div className="preview-section"><span className="preview-label">调度</span><span className="preview-value">{scheduleDescription(buildCreatePayload(taskForm).schedule)}</span></div>
            <div className="preview-section"><span className="preview-label">触发类型</span><span className="preview-value">{taskForm.trigger_type === 'unconfigured' ? '待补充条件' : taskForm.trigger_type}</span></div>
            {taskForm.trigger_type === 'sql' && <div className="preview-section"><span className="preview-label">触发 SQL</span><span className="preview-value">{taskForm.trigger_sql || '—'}</span></div>}
            {taskForm.trigger_type === 'agent' && <div className="preview-section"><span className="preview-label">触发 Agent</span><span className="preview-value">{taskForm.trigger_agent}</span></div>}
            {taskForm.trigger_prompt && <div className="preview-section"><span className="preview-label">触发提示词</span><span className="preview-value">{taskForm.trigger_prompt}</span></div>}
            <div className="preview-section"><span className="preview-label">报告 Agent</span><span className="preview-value">{taskForm.report_agent}</span></div>
            {taskForm.report_prompt && <div className="preview-section"><span className="preview-label">报告提示词</span><span className="preview-value">{taskForm.report_prompt}</span></div>}
            {taskForm.email_enabled && <div className="preview-section"><span className="preview-label">邮件通知</span><span className="preview-value">收件人：{taskForm.email_to} · 抄送：{taskForm.email_cc || '无'} · 模板：{taskForm.email_template}<br/>CSV 数据集：{taskForm.csv_exports.join('、') || '不附加数据'}<br/>保存草稿不会发送邮件。</span></div>}
            <div className="preview-section"><span className="preview-label">负责人</span><span className="preview-value">{taskForm.owner || '—'}</span></div>
            <div className="preview-section"><span className="preview-label">状态</span><span className="preview-value draft">草稿（不会自动执行）</span></div>
            <div className="form-actions"><button className="secondary-button" disabled={taskSaving} onClick={() => setShowPreview(false)}>返回编辑</button><button className="primary-button" onClick={() => void saveTask()} disabled={taskSaving}>{taskSaving ? '保存中…' : '确认保存为草稿'}</button></div>
          </div>}
        </div></div>}

        {deleteTarget && <div className="modal-overlay" onClick={() => setDeleteTarget(null)}><div className="modal-content small" onClick={e => e.stopPropagation()}><div className="modal-header"><h2>删除草稿</h2><button className="icon-button" onClick={() => setDeleteTarget(null)}><X size={18}/></button></div><p className="modal-body">确定删除任务「{taskName(deleteTarget, 0)}」？此操作不可撤销。</p><div className="form-actions"><button className="secondary-button" onClick={() => setDeleteTarget(null)}>取消</button><button className="primary-button danger" onClick={() => void deleteDraftTask(deleteTarget)}>确认删除</button></div></div></div>}

        {page === 'reports' && <Reports key={reportTaskId} request={apiRequest} initialTaskId={reportTaskId} trialSettings={trialSettings}/>}
      </main>
    </div>
  )
}

function AuthenticatedApp() {
  const [session, setSession] = useState<{user: string; mode: string} | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    void apiRequest<{user: string; mode: string}>('/api/session').then(setSession)
      .catch(error => setError(error instanceof Error ? error.message : '登录不可用'))
  }, [])
  if (!session) return <main className="content-state"><ShieldCheck size={36}/><h1>Business AI Hub</h1>
    <p>{error || '正在确认登录身份…'}</p>{error && <a className="primary-button" href="/auth/login">使用 Databricks 账户登录</a>}
    <small>任务、报告和数据访问使用你的账户权限。</small></main>
  return <><App userLabel={session.user} authMode={session.mode}/>{session.mode !== 'local_cli' &&
    <button className="secondary-button" style={{position:'fixed',bottom:12,right:12}} onClick={() => {
      void apiRequest('/api/session/logout',{method:'POST'}).then(() => window.location.assign('/'))
    }}>退出登录</button>}</>
}

export default AuthenticatedApp
