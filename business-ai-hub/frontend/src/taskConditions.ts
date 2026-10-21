import type { TaskFormState } from './taskDrafts'

export type SQLSuggestion = { sql: string; review: Record<string, unknown>; warnings: string[] }

export function conditionWindow(form: TaskFormState): string {
  const review = form.trigger_type === 'agent' ? form.agent_review : form.sql_review
  if (typeof review.window === 'string') return review.window
  const condition = form.trigger_prompt || form.draft_review?.original_request || ''
  if (/前一日|前一天|昨天|昨日|\b(yesterday|previous day)\b/i.test(condition)
      && /上周|上一周|\b(last week|previous week)\b/i.test(condition)) return 'day_vs_week'
  return form.schedule_freq === 'weekly' ? 'week' : 'day'
}

export function conditionAgents(form: TaskFormState): string[] {
  return form.trigger_agent ? ['genie_one'] : []
}

export function sqlGenerationRequest(form: TaskFormState, businessDate = '') {
  return { condition: form.trigger_prompt.trim() || form.draft_review?.original_request || form.description,
    original_request: form.draft_review?.original_request || '', agent: form.trigger_agent,
    conversation: form.draft_review?.conversation || [],
    context_agents: conditionAgents(form), window: conditionWindow(form),
    ...(businessDate ? { business_date: businessDate } : {}) }
}

export function applySQLSuggestion(form: TaskFormState, data: SQLSuggestion): TaskFormState {
  return { ...form, trigger_type: 'sql', trigger_sql: data.sql, sql_review: data.review,
    draft_review: form.draft_review ? { ...form.draft_review,
      warnings: [...new Set([...form.draft_review.warnings, ...data.warnings])].slice(0,20),
      missing_information: form.draft_review.missing_information.map(item => item === '需用 Genie 生成或手动提供 SQL，再审核并运行一次只读条件测试。' ? '需审核生成的 SQL 并运行一次只读条件测试。' : item),
    } : null }
}
