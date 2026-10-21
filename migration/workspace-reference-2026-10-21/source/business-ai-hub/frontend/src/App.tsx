import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Activity, ArrowRight, BarChart3, CalendarDays, Check, ChevronDown,
  Clock3, ClipboardList, FileText, LayoutDashboard, Menu, MessageSquareText,
  Plus, RefreshCcw, Send, ShieldCheck, Sparkles, Target, X,
} from 'lucide-react'

type Page = 'chat' | 'tasks' | 'reports'
type Agent = 'genie_a1' | 'genie_a2'
type ChatMessage = { id: string; role: 'user' | 'assistant'; text: string; agent?: Agent }
type ApiTask = Record<string, unknown>

type ChatResponse = {
  agent?: string
  answer?: string
  status?: string
}

type TasksResponse = { tasks?: ApiTask[]; task_count?: number; yaml_count?: number; registry_count?: number }

type ScheduleFreq = 'daily' | 'weekly' | 'interval'
type TriggerType = 'always' | 'sql' | 'agent'

type TaskFormState = {
  task_id: string
  name: string
  description: string
  schedule_freq: ScheduleFreq
  schedule_cron: string
  interval_days: number
  trigger_type: TriggerType
  trigger_sql: string
  trigger_agent: string
  trigger_prompt: string
  report_agent: string
  report_prompt: string
  owner: string
}

const EMPTY_FORM: TaskFormState = {
  task_id: '', name: '', description: '',
  schedule_freq: 'daily', schedule_cron: '', interval_days: 1,
  trigger_type: 'always', trigger_sql: '', trigger_agent: 'genie_a1', trigger_prompt: '',
  report_agent: 'genie_a1', report_prompt: '', owner: '',
}

const AGENTS: Record<Agent, { name: string; short: string; subtitle: string; icon: 'sales' | 'market' }> = {
  genie_a1: {
    name: '销售与运营分析',
    short: '销售分析',
    subtitle: 'GMV、销售表现、渠道与品牌运营',
    icon: 'sales',
  },
  genie_a2: {
    name: '竞品与市场分析',
    short: '竞品分析',
    subtitle: '市场趋势、竞品表现与市场份额',
    icon: 'market',
  },
}

const SUGGESTIONS: Record<Agent, string[]> = {
  genie_a1: [
    '2025年6月善存的GMV是多少？请注明指标口径。',
    '按月份总结2025年上半年的销售趋势。',
    '对比善存和钙尔奇在2025年6月的销售表现。',
    '哪些渠道的销售表现最突出？',
  ],
  genie_a2: [
    '分析2025年上半年主要品牌的竞争格局。',
    '钙尔奇与主要竞品的市场表现如何？',
    '2025年6月市场有哪些值得关注的变化？',
    '哪些品牌近期增长更快？',
  ],
}

const API_TIMEOUT_MS = 8 * 60 * 1000 + 15000

function toErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) return detail
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
  if (typeof value === 'string') return value.toLowerCase() === 'true'
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
    schedule: taskField(task, 'cron', 'frequency') || taskField(scheduleObj, 'cron', 'frequency', 'type', 'interval_days') || (typeof schedule === 'string' ? schedule : '未设置'),
    trigger: taskField(task, 'trigger_type') || taskField(triggerObj, 'type') || (typeof trigger === 'string' ? trigger : '未设置'),
    description: taskField(task, 'description', 'summary') || taskField(task, 'prompt', 'report_prompt') || '',
  }
}

const initialMessage: ChatMessage = {
  id: 'welcome', role: 'assistant',
  text: '你好！我是你的业务分析助手。你可以直接提问销售与运营问题，或者切换到竞品分析。\n\n以下分析基于当前连接的业务数据。',
  agent: 'genie_a1',
}

function App() {
  const [page, setPage] = useState<Page>('chat')
  const [agent, setAgent] = useState<Agent>('genie_a1')
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<Record<Agent, ChatMessage[]>>({ genie_a1: [initialMessage], genie_a2: [] })
  const [sending, setSending] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
  const [tasks, setTasks] = useState<ApiTask[]>([])
  const [tasksLoading, setTasksLoading] = useState(false)
  const [tasksError, setTasksError] = useState<string | null>(null)
  const [tasksFetched, setTasksFetched] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [agentPickerOpen, setAgentPickerOpen] = useState(false)
  const [showTaskForm, setShowTaskForm] = useState(false)
  const [showPreview, setShowPreview] = useState(false)
  const [taskForm, setTaskForm] = useState<TaskFormState>(EMPTY_FORM)
  const [taskSaving, setTaskSaving] = useState(false)
  const [taskError, setTaskError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ApiTask | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)

  const activeMessages = messages[agent]
  const countEnabled = tasks.filter(task => taskEnabled(task) === true).length
  const countDisabled = tasks.filter(task => taskEnabled(task) === false).length

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
    setTaskForm({ ...EMPTY_FORM, task_id: 'task_' + Date.now() })
    setTaskError(null)
    setShowTaskForm(true)
    setShowPreview(false)
  }

  function buildCreatePayload() {
    return {
      task_id: taskForm.task_id.trim(),
      name: taskForm.name.trim(),
      description: taskForm.description.trim(),
      enabled: false,
      schedule: {
        frequency: taskForm.schedule_freq,
        cron: taskForm.schedule_cron.trim(),
        interval_days: taskForm.schedule_freq === 'interval' ? taskForm.interval_days : 0,
      },
      trigger: {
        type: taskForm.trigger_type,
        sql: taskForm.trigger_type === 'sql' ? taskForm.trigger_sql.trim() : '',
        agent: taskForm.trigger_type === 'agent' ? taskForm.trigger_agent : '',
        prompt: taskForm.trigger_prompt.trim(),
      },
      report: {
        agent: taskForm.report_agent,
        prompt: taskForm.report_prompt.trim(),
        template: '',
      },
      owner: taskForm.owner.trim(),
    }
  }

  async function saveTask() {
    if (!taskForm.task_id.trim() || !taskForm.name.trim()) {
      setTaskError('任务 ID 和名称不能为空。')
      return
    }
    setTaskSaving(true)
    setTaskError(null)
    try {
      await apiRequest<{ status: string }>('/api/tasks/create', {
        method: 'POST', body: JSON.stringify(buildCreatePayload()),
      }, 60000)
      setShowTaskForm(false)
      setShowPreview(false)
      setTaskForm(EMPTY_FORM)
      await fetchTasks()
    } catch (error) {
      setTaskError(error instanceof Error ? error.message : '保存失败。')
    } finally {
      setTaskSaving(false)
    }
  }

  async function deleteDraftTask(task: ApiTask) {
    const tid = taskField(task, 'task_id')
    if (!tid) return
    try {
      await apiRequest<{ status: string }>(`/api/tasks/${tid}`, {
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

  function taskStatus(task: ApiTask): string {
    const status = task.lifecycle_status
    return typeof status === 'string' ? status : 'active'
  }

  async function ask(question: string) {
    const text = question.trim()
    if (!text || sending) return
    const selectedAgent = agent
    setChatError(null)
    setDraft('')
    setMessages(previous => ({
      ...previous,
      [selectedAgent]: [...previous[selectedAgent], { id: crypto.randomUUID(), role: 'user', text }],
    }))
    setSending(true)
    try {
      const data = await apiRequest<ChatResponse>('/api/chat', {
        method: 'POST', body: JSON.stringify({ question: text, agent: selectedAgent }),
      }, API_TIMEOUT_MS)
      if (typeof data.answer !== 'string' || !data.answer.trim()) {
        throw new Error('Genie 未返回有效的分析内容。')
      }
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

  function navigate(next: Page) {
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
          <div className="workspace-card"><div className="workspace-card-icon"><ShieldCheck size={17}/></div><div><strong>Databricks Workspace</strong><p>安全连接 · App Authorization</p></div></div>
          <div className="sidebar-footer"><span className="online-indicator" />服务已连接 <span className="version">v0.1</span></div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="打开菜单"><Menu size={21}/></button>
          <div className="breadcrumbs"><span>工作空间</span><span className="crumb-separator">/</span><strong>{page === 'chat' ? 'AI 分析' : page === 'tasks' ? '我的任务' : '分析报告'}</strong></div>
          <div className="topbar-right"><span className="environment-pill"><span className="online-indicator"/> App running</span><span className="avatar">AI</span></div>
        </header>

        {page === 'chat' && <section className="chat-page">
          <div className="page-heading chat-heading"><div><div className="eyebrow"><Sparkles size={14}/> BUSINESS INTELLIGENCE</div><h1>与数据对话</h1><p>用自然语言提问，让业务数据给出清晰的答案。</p></div></div>
          <div className="agent-row"><div className="agent-selector" ref={pickerRef}>
            <button className="agent-trigger" onClick={() => setAgentPickerOpen(!agentPickerOpen)} aria-expanded={agentPickerOpen} aria-haspopup="listbox" disabled={sending}>
              <span className={`agent-icon ${agent === 'genie_a1' ? 'sales' : 'market'}`}>{agent === 'genie_a1' ? <BarChart3 size={20}/> : <Target size={20}/>}</span>
              <span className="agent-trigger-copy"><span className="agent-overline">当前分析助手</span><strong>{AGENTS[agent].name}</strong></span><ChevronDown size={17} className="chevron"/>
            </button>
            {agentPickerOpen && <div className="agent-menu" role="listbox" aria-label="选择分析助手">{(Object.keys(AGENTS) as Agent[]).map(key => <button key={key} role="option" aria-selected={key === agent} className="agent-option" onClick={() => chooseAgent(key)}><span className={`agent-icon ${key === 'genie_a1' ? 'sales' : 'market'}`}>{key === 'genie_a1' ? <BarChart3 size={18}/> : <Target size={18}/>}</span><span><strong>{AGENTS[key].name}</strong><small>{AGENTS[key].subtitle}</small></span>{key === agent && <Check size={17} className="option-check"/>}</button>)}</div>}
          </div><span className="powered-by"><span className="online-indicator"/> Connected to Databricks Genie</span></div>

          <div className="chat-panel">
            <div className="conversation" role="log" aria-label="对话记录" aria-live="polite">
              {activeMessages.length === 0 && <div className="blank-conversation"><span className="empty-orb"><Sparkles size={28}/></span><h2>开始探索市场数据</h2><p>选择下面的问题，或者提出你自己的业务问题。</p></div>}
              {activeMessages.map(message => <div key={message.id} className={`chat-message ${message.role}`}>
                <div className={`message-avatar ${message.role === 'assistant' ? 'assistant-avatar' : 'user-avatar'}`}>{message.role === 'assistant' ? <Sparkles size={17}/> : '你'}</div>
                <div className="message-content"><span className="speaker-name">{message.role === 'assistant' ? AGENTS[message.agent || agent].name : '你'}</span><div className={`message-bubble ${message.role}`}>
                  {message.role === 'assistant' ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text}</ReactMarkdown> : <p>{message.text}</p>}
                </div></div>
              </div>)}
              {sending && <div className="chat-message assistant"><div className="message-avatar assistant-avatar"><Sparkles size={17}/></div><div className="message-content"><span className="speaker-name">{AGENTS[agent].name}</span><div className="thinking"><span className="loading-dots"><i/><i/><i/></span><span>正在分析数据，这可能需要一些时间…</span></div></div></div>}
              <div ref={bottomRef}/>
            </div>
            {activeMessages.length < 3 && !sending && <div className="suggestions"><div className="suggestions-label"><Sparkles size={14}/> 试试这样提问</div><div className="suggestions-grid">{SUGGESTIONS[agent].map((suggestion, i) => <button className="suggestion" key={i} onClick={() => void ask(suggestion)}><span>{suggestion}</span><ArrowRight size={16}/></button>)}</div></div>}
            {chatError && <div className="error-banner" role="alert"><span>{chatError}</span><button onClick={() => setChatError(null)} aria-label="关闭错误"><X size={15}/></button></div>}
            <form className="composer" onSubmit={onSubmit}><div className="composer-field"><textarea aria-label="输入业务问题" placeholder="向业务数据提问…" value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={onKeyDown} rows={2} disabled={sending}/><button type="submit" className="send-button" disabled={!draft.trim() || sending} title="发送消息"><Send size={18}/></button></div><div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span><span>AI 回答请结合实际业务判断</span></div></form>
          </div>
        </section>}

        {page === 'tasks' && <section className="content-page"><div className="page-heading"><div><div className="eyebrow"><Activity size={14}/> TASK AUTOMATION</div><h1>我的任务</h1><p>管理任务配置，创建草稿并预览。</p></div><div className="heading-actions"><button className="primary-button" onClick={openTaskForm}><Plus size={16}/>创建任务</button><button className="secondary-button" onClick={() => void fetchTasks()} disabled={tasksLoading}><RefreshCcw size={16} className={tasksLoading ? 'spin' : ''}/>刷新</button></div></div>
          <div className="metric-grid"><div className="metric-card"><span>任务总数</span><div><strong>{tasksFetched ? tasks.length : '—'}</strong><span className="metric-icon grey"><ClipboardList size={20}/></span></div><p>已配置的自动化任务</p></div><div className="metric-card"><span>已启用</span><div><strong>{tasksFetched ? countEnabled : '—'}</strong><span className="metric-icon green"><Check size={20}/></span></div><p>按原始配置执行</p></div><div className="metric-card"><span>已停用</span><div><strong>{tasksFetched ? countDisabled : '—'}</strong><span className="metric-icon amber"><Clock3 size={20}/></span></div><p>当前不会触发</p></div></div>
          <div className="section-title"><div><h2>任务列表</h2><span className="count-pill">{tasksFetched ? tasks.length : '—'}</span></div><span className="section-subtitle">来源：Databricks Workspace · 实时配置</span></div>
          {tasksLoading && <div className="content-state"><span className="spinner"/>正在加载任务…</div>}
          {tasksError && <div className="content-state error-state"><p>无法读取任务列表</p><small>{tasksError}</small><button className="secondary-button" onClick={() => void fetchTasks()}><RefreshCcw size={15}/>重试</button></div>}
          {!tasksLoading && !tasksError && tasksFetched && tasks.length === 0 && <div className="content-state"><ClipboardList size={26}/><p>暂无可显示的任务</p><small>请确认 Task Manager 配置目录中存在任务。</small></div>}
          {!tasksLoading && !tasksError && tasksFetched && tasks.length > 0 && <div className="task-list">{tasks.map((task, index) => {
            const meta = taskMeta(task)
            const enabled = taskEnabled(task)
            const source = taskSource(task)
            const status = taskStatus(task)
            const isDraft = status === 'draft'
            return <div className="task-row" key={taskField(task, '_config_path', 'id', 'task_id') || index}><div className="task-identity"><div className="task-icon"><CalendarDays size={20}/></div><div><h3>{taskName(task, index).replaceAll('_', ' ')}</h3><p>{meta.description || `触发方式：${meta.trigger}`}</p></div></div><div className="task-details"><span><Clock3 size={15}/> {meta.schedule}</span><span><Activity size={15}/> {meta.trigger}</span><span className={`source-badge ${source}`}>{source === 'yaml' ? 'YAML' : 'Registry'}</span></div><div className="task-actions"><span className={`status-chip ${isDraft ? 'draft' : enabled === true ? 'enabled' : enabled === false ? 'disabled' : 'unknown'}`}><i/>{isDraft ? '草稿' : enabled === true ? '已启用' : enabled === false ? '已停用' : '状态未知'}</span>{isDraft && <button className="icon-button delete" onClick={() => setDeleteTarget(task)} title="删除草稿"><X size={15}/></button>}</div></div>
          })}</div>}
          <div className="info-note"><ShieldCheck size={17}/><span>YAML 任务来源于现有 Task Manager 配置。Registry 任务为草稿，不会自动执行。执行集成将在后续版本中接入。</span></div>
        </section>}

        {showTaskForm && <div className="modal-overlay" onClick={() => { setShowTaskForm(false); setShowPreview(false) }}><div className="modal-content" onClick={e => e.stopPropagation()}><div className="modal-header"><h2>{showPreview ? '预览任务配置' : '创建任务'}</h2><button className="icon-button" onClick={() => { setShowTaskForm(false); setShowPreview(false) }}><X size={18}/></button></div>

          {taskError && <div className="error-banner"><span>{taskError}</span><button onClick={() => setTaskError(null)}><X size={15}/></button></div>}

          {!showPreview && <div className="task-form">
            <div className="form-field"><label>任务 ID</label><input value={taskForm.task_id} onChange={e => setTaskForm(f => ({ ...f, task_id: e.target.value }))} placeholder="unique_task_id" /></div>
            <div className="form-field"><label>任务名称 *</label><input value={taskForm.name} onChange={e => setTaskForm(f => ({ ...f, name: e.target.value }))} placeholder="任务名称" /></div>
            <div className="form-field"><label>描述</label><textarea value={taskForm.description} onChange={e => setTaskForm(f => ({ ...f, description: e.target.value }))} rows={2} placeholder="任务描述" /></div>
            <div className="form-row"><div className="form-field"><label>调度频率</label><select value={taskForm.schedule_freq} onChange={e => setTaskForm(f => ({ ...f, schedule_freq: e.target.value as ScheduleFreq }))}><option value="daily">每日</option><option value="weekly">每周</option><option value="interval">间隔天数</option></select></div>{taskForm.schedule_freq === 'interval' && <div className="form-field"><label>间隔天数</label><input type="number" min={1} value={taskForm.interval_days} onChange={e => setTaskForm(f => ({ ...f, interval_days: parseInt(e.target.value) || 1 }))} /></div>}{taskForm.schedule_freq !== 'interval' && <div className="form-field"><label>Cron 表达式（可选）</label><input value={taskForm.schedule_cron} onChange={e => setTaskForm(f => ({ ...f, schedule_cron: e.target.value }))} placeholder="0 8 * * *" /></div>}</div>
            <div className="form-row"><div className="form-field"><label>触发类型</label><select value={taskForm.trigger_type} onChange={e => setTaskForm(f => ({ ...f, trigger_type: e.target.value as TriggerType }))}><option value="always">always</option><option value="sql">sql</option><option value="agent">agent</option></select></div>{taskForm.trigger_type === 'agent' && <div className="form-field"><label>触发 Agent</label><select value={taskForm.trigger_agent} onChange={e => setTaskForm(f => ({ ...f, trigger_agent: e.target.value }))}><option value="genie_a1">genie_a1</option><option value="genie_a2">genie_a2</option></select></div>}</div>
            {taskForm.trigger_type === 'sql' && <div className="form-field"><label>触发 SQL 条件</label><textarea value={taskForm.trigger_sql} onChange={e => setTaskForm(f => ({ ...f, trigger_sql: e.target.value }))} rows={2} placeholder="SELECT ..." /></div>}
            {taskForm.trigger_type !== 'always' && <div className="form-field"><label>触发提示词</label><textarea value={taskForm.trigger_prompt} onChange={e => setTaskForm(f => ({ ...f, trigger_prompt: e.target.value }))} rows={2} placeholder="触发时执行的提示词" /></div>}
            <div className="form-row"><div className="form-field"><label>报告 Agent</label><select value={taskForm.report_agent} onChange={e => setTaskForm(f => ({ ...f, report_agent: e.target.value }))}><option value="genie_a1">genie_a1</option><option value="genie_a2">genie_a2</option></select></div><div className="form-field"><label>负责人</label><input value={taskForm.owner} onChange={e => setTaskForm(f => ({ ...f, owner: e.target.value }))} placeholder="任务负责人" /></div></div>
            <div className="form-field"><label>报告提示词</label><textarea value={taskForm.report_prompt} onChange={e => setTaskForm(f => ({ ...f, report_prompt: e.target.value }))} rows={2} placeholder="报告生成时的提示词" /></div>
            <div className="form-actions"><button className="secondary-button" onClick={() => { setShowTaskForm(false); setTaskForm(EMPTY_FORM) }}>取消</button><button className="primary-button" onClick={() => setShowPreview(true)} disabled={!taskForm.task_id.trim() || !taskForm.name.trim()}>预览并保存</button></div>
          </div>}

          {showPreview && <div className="task-preview">
            <div className="preview-section"><span className="preview-label">任务 ID</span><span className="preview-value">{taskForm.task_id}</span></div>
            <div className="preview-section"><span className="preview-label">名称</span><span className="preview-value">{taskForm.name}</span></div>
            <div className="preview-section"><span className="preview-label">描述</span><span className="preview-value">{taskForm.description || '—'}</span></div>
            <div className="preview-section"><span className="preview-label">调度</span><span className="preview-value">{taskForm.schedule_freq === 'interval' ? `每 ${taskForm.interval_days} 天` : taskForm.schedule_freq === 'daily' ? '每日' : '每周'}{taskForm.schedule_cron ? ` (${taskForm.schedule_cron})` : ''}</span></div>
            <div className="preview-section"><span className="preview-label">触发类型</span><span className="preview-value">{taskForm.trigger_type}</span></div>
            {taskForm.trigger_type === 'sql' && <div className="preview-section"><span className="preview-label">触发 SQL</span><span className="preview-value">{taskForm.trigger_sql || '—'}</span></div>}
            {taskForm.trigger_type === 'agent' && <div className="preview-section"><span className="preview-label">触发 Agent</span><span className="preview-value">{taskForm.trigger_agent}</span></div>}
            {taskForm.trigger_prompt && <div className="preview-section"><span className="preview-label">触发提示词</span><span className="preview-value">{taskForm.trigger_prompt}</span></div>}
            <div className="preview-section"><span className="preview-label">报告 Agent</span><span className="preview-value">{taskForm.report_agent}</span></div>
            {taskForm.report_prompt && <div className="preview-section"><span className="preview-label">报告提示词</span><span className="preview-value">{taskForm.report_prompt}</span></div>}
            <div className="preview-section"><span className="preview-label">负责人</span><span className="preview-value">{taskForm.owner || '—'}</span></div>
            <div className="preview-section"><span className="preview-label">状态</span><span className="preview-value draft">草稿（不会自动执行）</span></div>
            <div className="form-actions"><button className="secondary-button" onClick={() => setShowPreview(false)}>返回编辑</button><button className="primary-button" onClick={() => void saveTask()} disabled={taskSaving}>{taskSaving ? '保存中…' : '确认保存为草稿'}</button></div>
          </div>}
        </div></div>}

        {deleteTarget && <div className="modal-overlay" onClick={() => setDeleteTarget(null)}><div className="modal-content small" onClick={e => e.stopPropagation()}><div className="modal-header"><h2>删除草稿</h2><button className="icon-button" onClick={() => setDeleteTarget(null)}><X size={18}/></button></div><p className="modal-body">确定删除任务「{taskName(deleteTarget, 0)}」？此操作不可撤销。</p><div className="form-actions"><button className="secondary-button" onClick={() => setDeleteTarget(null)}>取消</button><button className="primary-button danger" onClick={() => void deleteDraftTask(deleteTarget)}>确认删除</button></div></div></div>}

        {page === 'reports' && <section className="content-page"><div className="page-heading"><div><div className="eyebrow"><FileText size={14}/> REPORT CENTER</div><h1>分析报告</h1><p>集中查看自动化生成的业务报告。</p></div></div>
          <div className="reports-placeholder"><div className="placeholder-glow"><FileText size={34}/></div><span className="coming-soon">即将推出</span><h2>你的报告中心正在准备中</h2><p>后续版本将连接 Task Manager 的真实报告历史，按任务、日期与业务主题管理报告。</p><div className="roadmap-tags"><span><Check size={14}/> AI 分析已上线</span><span><Clock3 size={14}/> 报告历史待接入</span></div><button className="primary-button" onClick={() => navigate('chat')}>先用 AI 分析数据 <ArrowRight size={17}/></button></div>
          <div className="report-features"><div><LayoutDashboard size={19}/><strong>集中查看</strong><p>按任务归档每次实际生成的报告</p></div><div><CalendarDays size={19}/><strong>历史追踪</strong><p>回顾报告内容与生成时间</p></div><div><Plus size={19}/><strong>按需扩展</strong><p>未来支持自定义报告模板和计划</p></div></div>
        </section>}
      </main>
    </div>
  )
}

export default App
