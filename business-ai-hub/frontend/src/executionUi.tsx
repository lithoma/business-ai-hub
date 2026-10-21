import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { scheduleDescription, type DraftReview } from './taskDrafts'
import { ReportMail, type TrialSettings } from './ManualTrial'

type Request = <T>(url: string, options?: RequestInit, timeoutMs?: number) => Promise<T>
type Task = Record<string, unknown>
type Readiness = { errors: string[]; warnings: string[]; execution_enabled: boolean; can_activate: boolean; updated_at: string; configuration?: Task }

export function TaskLifecycle({ task, request, onChanged, onEdit }: { task: Task; request: Request; onChanged: () => Promise<void>; onEdit: () => void }) {
  const [open, setOpen] = useState(false)
  const [ready, setReady] = useState<Readiness | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const status = String(task.lifecycle_status || 'draft')
  const storedTask = ready?.configuration || task
  const schedule = (storedTask.schedule || {}) as Record<string, unknown>
  const trigger = (storedTask.trigger || {}) as Record<string, unknown>
  const report = (storedTask.report || {}) as Record<string, unknown>
  const delivery = report.delivery as { enabled?: boolean; to?: string[]; cc?: string[]; template_id?: string; csv_exports?: string[] } | undefined
  const review = report.draft_review as DraftReview | null | undefined
  const action = status === 'active' ? 'pause' : status === 'paused' ? 'edit' : 'activate'
  const label = action === 'pause' ? '暂停任务' : action === 'edit' ? '转回草稿' : '审核启用'
  const url = `/api/tasks/${encodeURIComponent(String(task.task_id))}`

  async function inspect() {
    setOpen(true); setError(''); setReviewed(false); setReady(null); setBusy(true)
    try { setReady(await request<Readiness>(url + '/readiness', undefined, 60000)) }
    catch (e) { setError(e instanceof Error ? e.message : '无法读取任务') }
    finally { setBusy(false) }
  }
  async function confirm() {
    if (!ready || busy) return
    setBusy(true); setError('')
    try {
      await request(url + '/lifecycle', { method: 'POST', body: JSON.stringify({
        action, confirm: true, reviewed, expected_updated_at: ready.updated_at,
      }) }, 60000)
      setOpen(false)
      await onChanged()
    } catch (e) { setError(e instanceof Error ? e.message : '状态变更失败，请刷新核对。') }
    finally { setBusy(false) }
  }
  return <>
    {status === 'draft' && <button className="secondary-button" onClick={onEdit}>编辑草稿</button>}
    <button className="secondary-button" onClick={() => void inspect()}>{label}</button>
    {open && <div className="modal-overlay"><div className="modal-content small" role="dialog" aria-modal="true" aria-label={label}>
      <div className="modal-header"><h2>{label}：{String(storedTask.name)}</h2><button className="icon-button" disabled={busy} onClick={() => setOpen(false)} aria-label="关闭审核">×</button></div>
      <div className="modal-body">
        <p>{action === 'activate' ? '确认调度、触发条件、报告提示词及生成说明。启用只改变 Registry 状态，不会启动 Job。' : action === 'pause' ? '暂停后，后续批次会跳过此任务；已经开始的执行仍可能完成。' : '转回草稿后可以修改配置，再重新审核启用。'}</p>
        {busy && <p role="status">正在处理…</p>}
        {error && <p role="alert">{error}</p>}
        {ready && action === 'activate' && <>
          <section className="activation-config" aria-label="待审核任务配置">
            <p><strong>调度：</strong>{scheduleDescription(schedule)}</p>
            <p><strong>触发方式：</strong>{trigger.type === 'always' ? '按期无条件生成' : trigger.type === 'agent' ? '由 Agent 判断条件' : trigger.type === 'sql' ? 'SQL 条件' : '待补充'}</p>
            {trigger.prompt ? <p><strong>判断条件：</strong>{String(trigger.prompt)}</p> : null}
            {trigger.sql ? <pre>{String(trigger.sql)}</pre> : null}
            <p><strong>报告 Agent：</strong>{String(report.agent || '未设置')}</p>
            <p><strong>报告要求：</strong>{String(report.prompt || '未设置')}</p>
            {delivery?.enabled && <p><strong>邮件通知：</strong>收件人 {delivery.to?.join('、')} · 抄送 {delivery.cc?.join('、') || '无'} · 模板 {delivery.template_id} · CSV {delivery.csv_exports?.join('、') || '无'}</p>}
            {review && <><p><strong>原始需求：</strong>{review.original_request}</p>
              {!!review.conversation?.length && <details className="conversation-context"><summary>带入的业务讨论（{review.conversation.length} 条）</summary>{review.conversation.map((turn,index) => <p key={index}><strong>{turn.role === 'user' ? '你' : 'Genie'}：</strong>{turn.content}</p>)}</details>}
              {([['生成假设', review.assumptions], ['注意事项', review.warnings], ['待补充信息', review.missing_information]] as const).map(([title, items]) => items.length ? <div key={title}><strong>{title}</strong><ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul></div> : null)}</>}
          </section>
          {ready.errors.length > 0 && <ul role="alert">{ready.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
          {!ready.execution_enabled && <p>执行集成尚未开放；请完成部署验收后启用。当前草稿会保留。</p>}
          <ul>{ready.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          <label><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)}/> 我已审核配置、生成假设和待补充信息</label>
        </>}
      </div>
      <div className="form-actions"><button className="secondary-button" disabled={busy} onClick={() => setOpen(false)}>取消</button>
        <button className="primary-button" disabled={busy || !ready || (action === 'activate' && (!ready.can_activate || !reviewed))} onClick={() => void confirm()}>确认{label}</button></div>
    </div></div>}
  </>
}

type Run = { run_id: string; task_id: string; task_name: string; source?: string; status: string; scheduled_slot: string; started_at: string; finished_at: string; trigger_reason: string; report_text: string; error_code: string; notification_status: string; notification_error_code?: string; provider_message_id?: string; artifacts_json?: string }
const STATUS: Record<string, string> = { RUNNING: '执行中（长时间未完成请检查）', REPORT_CREATED: '已生成报告', NOT_TRIGGERED: '条件未满足', TRIGGER_CONFIRMED: '条件成立（仅测试，未生成报告）', FAILED: '执行失败' }

export function Reports({ request, initialTaskId = '', trialSettings }: { request: Request; initialTaskId?: string; trialSettings?: TrialSettings }) {
  const [runs, setRuns] = useState<Run[]>([])
  const [status, setStatus] = useState('')
  const [task, setTask] = useState(initialTaskId)
  const [offset, setOffset] = useState<number | null>(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [appliedFilters, setAppliedFilters] = useState({ status: '', task: '' })
  async function load(start = 0) {
    setBusy(true); setError('')
    try {
      const filters = start ? appliedFilters : { status, task: task.trim() }
      const params = new URLSearchParams({ status: filters.status, task_id: filters.task, offset: String(start) })
      const data = await request<{ runs: Run[]; next_offset: number | null }>('/api/reports?' + params, undefined, 60000)
      if (!Array.isArray(data.runs)) throw new Error('报告数据格式无效。')
      setRuns(previous => start ? [...previous, ...data.runs] : data.runs)
      if (!start) setAppliedFilters(filters)
      setOffset(data.next_offset); setLoaded(true)
    } catch (e) { setError(e instanceof Error ? e.message : '无法读取报告历史。') }
    finally { setBusy(false) }
  }
  useEffect(() => { void load() }, []) // Initial read only; filters applied explicitly.
  return <section className="content-page"><div className="page-heading"><div><h1>分析报告</h1><p>查看实际执行结果、条件判断、失败记录与报告内容。</p></div></div>
    <form className="report-filters" onSubmit={e => { e.preventDefault(); void load() }}>
      <label>任务 ID<input value={task} onChange={e => setTask(e.target.value)} disabled={busy}/></label>
      <label>执行状态<select value={status} onChange={e => setStatus(e.target.value)} disabled={busy}><option value="">全部状态</option>{Object.entries(STATUS).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label>
      <button className="secondary-button" disabled={busy}>查询 / 刷新</button>
    </form>
    <div className="info-note">邮件服务已接收不代表已送达。发送结果不确定时请先核对发件记录，避免重复发送；旧版通知入队状态需要检查 SQL Alert。</div>
    {error && <div className="error-banner" role="alert">{error}</div>}
    {busy && <p role="status">正在读取执行记录…</p>}
    {loaded && !busy && !error && runs.length === 0 && <div className="content-state">暂无执行记录。保存草稿不会生成报告。</div>}
    {!error && <div className="run-list">{runs.map(run => <article className="run-card" key={run.run_id}>
      <h2>{run.task_name || run.task_id}</h2><p>{STATUS[run.status] || run.status} · {run.source === 'manual_local' ? `手动测试 · 业务日期 ${run.scheduled_slot.slice(-10)}` : run.scheduled_slot}</p>
      <small>开始：{run.started_at} · 完成：{run.finished_at || '—'}</small>
      {run.trigger_reason && <p>条件判断：{run.trigger_reason}</p>}
      {run.error_code && <p role="alert">失败类型：{run.error_code}；{run.source === 'manual_local' ? '请核对业务日期、数据和本地试运行状态。' : '请检查 Job 日志和 Agent 配置。'}</p>}
      <p>通知：{({ QUEUED: '已入队（未确认送达）', EMAIL_SENDING: '邮件发送处理中（中断后需核对）', EMAIL_ACCEPTED: '邮件服务已接收（未确认送达）', EMAIL_FAILED: '邮件发送失败', EMAIL_UNKNOWN: '发送结果不确定，请先核对发件记录' } as Record<string, string>)[run.notification_status] || '未入队'}</p>
      {run.notification_error_code && <p>邮件错误：{run.notification_error_code}</p>}
      {run.provider_message_id && <small>邮件记录：{run.provider_message_id}</small>}
      {run.report_text && <details><summary>查看报告</summary><div className="report-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{run.report_text}</ReactMarkdown></div></details>}
      {run.source === 'manual_local' && run.status === 'REPORT_CREATED' && <ReportMail runId={run.run_id} settings={trialSettings} request={request} onChanged={() => void load()}/>}
    </article>)}</div>}
    {loaded && offset !== null && !error && <button className="secondary-button" disabled={busy} onClick={() => void load(offset)}>加载更多</button>}
  </section>
}
