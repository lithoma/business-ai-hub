export type ScheduleFreq = 'daily' | 'weekly' | 'interval'
export type TriggerType = 'always' | 'sql' | 'agent' | 'unconfigured'
export const WEEKDAYS = [
  ['monday', '周一'], ['tuesday', '周二'], ['wednesday', '周三'],
  ['thursday', '周四'], ['friday', '周五'], ['saturday', '周六'], ['sunday', '周日'],
] as const

export type DraftReview = {
  original_request: string
  conditional: boolean
  assumptions: string[]
  warnings: string[]
  missing_information: string[]
  conversation?: ConversationTurn[]
  source_agent?: 'genie_one' | null
}

export type ConversationTurn = { role: 'user' | 'assistant'; content: string }

export type TaskFormState = {
  task_id: string; name: string; description: string
  schedule_freq: ScheduleFreq; schedule_time: string; schedule_timezone: string
  schedule_weekday: string; schedule_start_date: string; interval_days: number
  trigger_type: TriggerType; trigger_sql: string; trigger_agent: string; trigger_prompt: string
  condition_agents: string[]
  report_agent: string; report_prompt: string; report_template: string; owner: string
  draft_review: DraftReview | null
  sql_review: Record<string, unknown>
  agent_review: Record<string, unknown>
  email_enabled: boolean; email_to: string; email_cc: string; email_template: string; email_subject: string; csv_exports: string[]
}

export const EMPTY_FORM: TaskFormState = {
  task_id: '', name: '', description: '',
  schedule_freq: 'daily', schedule_time: '09:00', schedule_timezone: 'Asia/Shanghai',
  schedule_weekday: 'monday', schedule_start_date: '', interval_days: 1,
  trigger_type: 'always', trigger_sql: '', trigger_agent: 'genie_one', trigger_prompt: '',
  condition_agents: ['genie_one'],
  report_agent: 'genie_one', report_prompt: '', report_template: '', owner: '', draft_review: null,
  sql_review: {},
  agent_review: {},
  email_enabled: false, email_to: '', email_cc: '', email_template: 'default', email_subject: '', csv_exports: [],
}

export type DraftConfiguration = {
  task_id: string; name: string; description: string; enabled: boolean; owner: string
  schedule: {
    frequency: ScheduleFreq; time: string; timezone: string; weekday: string
    interval_days: number; start_date: string; cron: string
  }
  trigger: { type: TriggerType; sql: string; agent: string; prompt: string; context_agents?: string[] }
  report: { agent: string; prompt: string; template: string; draft_review: DraftReview | null; condition_sql?: Record<string, unknown>; condition_agent?: Record<string, unknown>; delivery?: { enabled: boolean; to: string[]; cc: string[]; template_id: string; subject_template: string; csv_exports: string[] } }
}

export function proposalToForm(c: DraftConfiguration): TaskFormState {
  return {
    task_id: c.task_id, name: c.name, description: c.description, owner: c.owner,
    schedule_freq: c.schedule.frequency, schedule_time: c.schedule.time,
    schedule_timezone: c.schedule.timezone, schedule_weekday: c.schedule.weekday,
    schedule_start_date: c.schedule.start_date, interval_days: c.schedule.interval_days || 1,
    trigger_type: c.trigger.type, trigger_sql: c.trigger.sql,
    trigger_agent: c.trigger.agent || c.trigger.context_agents?.[0] || c.report.agent || 'genie_one', trigger_prompt: c.trigger.prompt,
    condition_agents: c.trigger.context_agents?.length ? c.trigger.context_agents : [c.trigger.agent || c.report.agent || 'genie_one'],
    report_agent: c.report.agent, report_prompt: c.report.prompt,
    report_template: c.report.template, draft_review: c.report.draft_review,
    sql_review: c.report.condition_sql || {},
    agent_review: c.report.condition_agent || {},
    email_enabled: c.report.delivery?.enabled || false, email_to: c.report.delivery?.to.join(', ') || '',
    email_cc: c.report.delivery?.cc.join(', ') || '', email_template: c.report.delivery?.template_id || 'default',
    email_subject: c.report.delivery?.subject_template || '', csv_exports: c.report.delivery?.csv_exports || [],
  }
}

export function scheduleDescription(s: { frequency?: unknown; time?: unknown; timezone?: unknown; weekday?: unknown; interval_days?: unknown; start_date?: unknown; cron?: unknown }): string {
  if (!s.time && s.cron) return `Cron ${s.cron}`
  const freq = s.frequency === 'weekly' ? `每${WEEKDAYS.find(([key]) => key === s.weekday)?.[1] || '周'}`
    : s.frequency === 'interval' ? `每 ${s.interval_days || '待定'} 天${s.start_date ? `，从 ${s.start_date} 开始` : '（开始日期待补充）'}`
    : s.frequency === 'daily' ? '每日' : '未设置'
  return `${freq}${s.time ? ` ${s.time}` : ''}${s.timezone ? ` · ${s.timezone}` : ''}`
}

export function emailAddresses(text: string) { return text.split(/[,;，；]/).map(value => value.trim()).filter(Boolean) }

export function buildCreatePayload(f: TaskFormState): DraftConfiguration {
  const [hours, minutes] = f.schedule_time.split(':').map(Number)
  const day = f.schedule_freq === 'weekly' ? (WEEKDAYS.findIndex(([key]) => key === f.schedule_weekday) + 1) % 7 : '*'
  return {
    task_id: f.task_id.trim(), name: f.name.trim(), description: f.description.trim(),
    enabled: false, owner: f.owner.trim(),
    schedule: {
      frequency: f.schedule_freq, time: f.schedule_time, timezone: f.schedule_timezone.trim(),
      weekday: f.schedule_weekday, interval_days: f.schedule_freq === 'interval' ? f.interval_days : 0,
      start_date: f.schedule_freq === 'interval' ? f.schedule_start_date : '',
      cron: f.schedule_freq === 'interval' ? '' : `${minutes} ${hours} * * ${day}`,
    },
    trigger: {
      type: f.trigger_type, sql: f.trigger_type === 'sql' ? f.trigger_sql.trim() : '',
      agent: f.trigger_type === 'agent' || f.trigger_type === 'sql' ? f.trigger_agent : '',
      context_agents: f.trigger_type === 'agent' || f.trigger_type === 'sql' ? [...new Set([f.trigger_agent,...f.condition_agents])] : [],
      prompt: f.trigger_type === 'always' ? '' : f.trigger_prompt.trim(),
    },
    report: { agent: f.report_agent, prompt: f.report_prompt.trim(), template: f.report_template, draft_review: f.draft_review, condition_sql: f.sql_review, condition_agent: f.agent_review,
      delivery: { enabled: f.email_enabled, to: emailAddresses(f.email_to), cc: emailAddresses(f.email_cc),
        template_id: f.email_template, subject_template: f.email_subject, csv_exports: f.csv_exports },
    },
  }
}

export function formError(f: TaskFormState): string | null {
  if (!f.task_id.trim() || !f.name.trim()) return '任务 ID 和名称不能为空。'
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(f.schedule_time)) return '请选择有效的检查时间。'
  try { new Intl.DateTimeFormat('zh-CN', { timeZone: f.schedule_timezone.trim() }) } catch { return '请选择有效的时区。' }
  if (f.schedule_freq === 'interval' && (!Number.isInteger(f.interval_days) || f.interval_days < 1 || f.interval_days > 365)) return '间隔天数必须为 1–365。'
  if (f.email_enabled && (!emailAddresses(f.email_to).length || [...emailAddresses(f.email_to), ...emailAddresses(f.email_cc)].some(address => !/^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$/.test(address)))) return '请填写有效的收件人/抄送邮箱。'
  if (f.csv_exports.length > 5) return '每封邮件最多选择 5 个 CSV 数据集。'
  if (f.draft_review?.conditional && f.trigger_type === 'always') return '原始需求包含触发条件，请保留条件或选择“待补充条件”。'
  return null
}

export function taskCounts(tasks: Record<string, unknown>[]) {
  let enabled = 0, disabled = 0, drafts = 0
  for (const task of tasks) {
    if (task.lifecycle_status === 'draft' || (task._source === 'registry' && !task.lifecycle_status)) { drafts++; continue }
    const config = task.config && typeof task.config === 'object' ? task.config as Record<string, unknown> : {}
    const value = task.enabled ?? task.is_enabled ?? config.enabled
    if (value === true || (typeof value === 'string' && value.trim().toLowerCase() === 'true')) enabled++
    if (value === false || (typeof value === 'string' && value.trim().toLowerCase() === 'false')) disabled++
  }
  return { total: tasks.length, enabled, disabled, drafts }
}


export function isRecurringTaskRequest(text: string): boolean {
  const cadence = /每(?:天|日|周|星期|隔)|every\s+(?:day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)/i.test(text)
  const action = /给我|发送|生成|创建|设置|提醒|监控|检查|\b(give|send|create|schedule|generate|notify|monitor|prepare|deliver|run)\b/i.test(text)
  const output = /报告|日报|周报|任务|监控|检查|\b(report|task|monitor|alert)\b/i.test(text)
  return cadence && action && output
}
