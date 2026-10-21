import { useState, type Dispatch, type SetStateAction } from 'react'
import type { TaskFormState } from './taskDrafts'
import { applySQLSuggestion, conditionAgents, conditionWindow, sqlGenerationRequest, type SQLSuggestion } from './taskConditions'
type Request = <T>(url: string, options?: RequestInit, timeoutMs?: number) => Promise<T>
type Review = Record<string, unknown> & { window?: string; bindings?: Record<string, string>; test?: { should_trigger: boolean; reason: string; tested_at: string; receipt: string } }

export function ConditionSQLFields({ form, setForm, request }: { form: TaskFormState; setForm: Dispatch<SetStateAction<TaskFormState>>; request: Request }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [businessDate, setBusinessDate] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const isAgent = form.trigger_type === 'agent'
  const reviewKey = isAgent ? 'agent_review' : 'sql_review'
  const review = form[reviewKey] as Review
  const window = conditionWindow(form)
  const agents = conditionAgents(form)
  const condition = form.trigger_prompt.trim() || form.draft_review?.original_request || form.description
  async function generate() {
    const agent = form.trigger_agent
    setBusy(true); setError(''); setWarnings([])
    try {
      const data = await request<SQLSuggestion>('/api/conditions/sql/generate', {
        method: 'POST', body: JSON.stringify(sqlGenerationRequest(form,businessDate)),
      }, 270000)
      setForm(current => current.trigger_agent === agent && conditionAgents(current).join() === agents.join() && conditionWindow(current) === window && (current.trigger_prompt.trim() || current.draft_review?.original_request || current.description) === condition
        ? applySQLSuggestion(current,data) : current)
      setWarnings(data.warnings)
    } catch (e) { setError(e instanceof Error ? e.message : 'SQL 生成失败') }
    finally { setBusy(false) }
  }
  async function test() {
    const sql = form.trigger_sql
    const agent = form.trigger_agent
    const mode = form.trigger_type
    setBusy(true); setError('')
    setForm(current => ({ ...current, [reviewKey]: { ...current[reviewKey], test: null } }))
    try {
      const data = await request<Review>(isAgent ? '/api/conditions/agent/test' : '/api/conditions/sql/test', { method: 'POST',
        body: JSON.stringify({ ...(isAgent ? { condition: form.trigger_prompt.trim(), agent, context_agents: agents } : { sql }), window, confirm: true, ...(businessDate ? { business_date: businessDate } : {}) }),
      }, isAgent ? 270000 : 65000)
      setForm(current => current.trigger_type === mode && current.trigger_agent === agent && conditionAgents(current).join() === agents.join() && conditionWindow(current) === window && current.trigger_sql === sql && (current.trigger_prompt.trim() || current.draft_review?.original_request || current.description) === condition ? { ...current, [reviewKey]: { ...current[reviewKey], ...data } } : current)
    } catch (e) { setError(e instanceof Error ? e.message : 'SQL 测试失败') }
    finally { setBusy(false) }
  }
  return <section className="condition-sql-tools" aria-label="触发条件工具">
    <strong>{isAgent ? 'AI 条件验证' : '触发 SQL'}</strong><p className="form-hint">数值阈值、同比环比、计数等优先用 SQL。只有需要语义或定性判断时才用 AI。缺少表结构或数据不应改选 AI。</p>
    <p className="form-hint">{isAgent ? '点击测试将由 Genie 查询数据并判断一次。数据不足或无法验证时显示错误；成功后请审核依据，测试不会保存或启用任务。' : 'Genie One 利用已有表结构、指标口径和示例生成 SQL，生成时会读取数据。生成不保存任务；请审核 SQL，再单独测试。'}</p>
    {!isAgent && !form.trigger_sql.trim() && <p role="status">触发 SQL 尚未生成。点击下方“用 Genie 生成 SQL”后，SQL 会填入上方编辑框。</p>}
    <div className="form-row"><div className="form-field"><label htmlFor="condition-sql-agent">业务知识来源</label><input id="condition-sql-agent" value="Genie One" readOnly /></div>
      <div className="form-field"><label htmlFor="condition-window">比较周期</label><select id="condition-window" value={window} disabled={busy} onChange={e => setForm(f => ({ ...f, [reviewKey]: { ...f[reviewKey], window: e.target.value, test: null } }))}><option value="week">最近完整自然周 / 前一自然周</option><option value="day">业务日期 / 前一日</option><option value="rolling7">最近 7 天 / 前 7 天</option><option value="day_vs_week">业务日期 / 上一完整自然周</option></select></div></div>
    <p className="form-hint">Genie One 根据指标口径选择业务资产，权限仍取决于当前用户。SQL 审核保存后直接重复执行，不再次调用 Genie 生成；修改条件时才重新生成或编辑。</p>
    <div className="form-field"><label htmlFor="condition-business-date">测试业务日期（可选）</label><input id="condition-business-date" type="date" value={businessDate} disabled={busy} onChange={e => { setBusinessDate(e.target.value); setForm(f => ({ ...f, [reviewKey]: { ...f[reviewKey], test: null } })) }}/><small>留空使用 Task Manager 的模拟业务日期设置；测试后显示实际日期参数。</small></div>
    <div className="heading-actions"><button type="button" className="secondary-button" disabled={busy || !condition.trim()} onClick={() => void generate()}>{busy ? '处理中…' : '用 Genie 生成 SQL（会查询数据）'}</button>
      <button type="button" className="secondary-button" disabled={busy || (isAgent ? !form.trigger_prompt.trim() : !form.trigger_sql.trim())} onClick={() => void test()}>{isAgent ? '运行一次 AI 条件测试（会查询数据）' : '运行一次只读条件测试'}</button></div>
    {error && <p role="alert">{error}</p>}
    {warnings.map((message, index) => <p className="form-hint" key={index}>{message}</p>)}
    {review.bindings && <p>业务日期：{review.bindings.business_date}；本期 {review.bindings.period_start} 至 {review.bindings.period_end}；前期 {review.bindings.previous_period_start} 至 {review.bindings.previous_period_end}</p>}
    {review.test && <p role="status">{isAgent ? 'AI 条件' : '只读条件'}测试成功：{review.test.should_trigger ? '条件成立' : '条件不成立'}。{review.test.reason}（这不会生成报告、启动 Job 或发送邮件。）</p>}
  </section>
}
