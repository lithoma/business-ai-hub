import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export type TrialSettings = { enabled: boolean; sender?: string; recipient?: string; mail_available?: boolean; api?: string }
const runPath = (settings?: TrialSettings) => settings?.api === 'runs' ? '/api/runs' : '/api/local-trials'
type Request = <T>(url: string, options?: RequestInit, timeout?: number) => Promise<T>
type Task = Record<string, unknown>
type Trial = { run_id: string; phase: string; status: string; error: string; trigger_reason: string;
  report_text: string; timings: Record<string, number>; notification_status: string }
type Preview = { sender: string; recipient: string; subject: string; body: string; preview_hash: string;
  can_send: boolean; notification_status: string; timings?: Record<string,number> }
const PHASES: Record<string,string> = { queued: '等待处理', condition: '正在检查触发条件', report: '正在生成报告', completed: '已完成', failed: '失败' }
const MAIL: Record<string,string> = { NOT_QUEUED: '尚未发送', EMAIL_SENDING: '发送处理中', EMAIL_ACCEPTED: '邮件服务已接收', EMAIL_FAILED: '发送失败', EMAIL_UNKNOWN: '发送结果不确定，请先核对发件记录' }

export function ReportMail({ runId, settings, request, onChanged }: {
  runId: string; settings?: TrialSettings; request: Request; onChanged?: () => void
}) {
  const [preview, setPreview] = useState<Preview>()
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submitting = useRef(false)
  if (!settings?.enabled) return null
  async function inspect() {
    if (submitting.current) return
    submitting.current = true; setBusy(true); setError(''); setConfirmed(false)
    try { setPreview(await request<Preview>(`${runPath(settings)}/${runId}/email-preview`, undefined, 30000)) }
    catch (e) { setError(e instanceof Error ? e.message : '无法预览邮件') }
    finally { submitting.current = false; setBusy(false) }
  }
  async function send() {
    if (!confirmed || !preview?.can_send || busy || submitting.current) return
    submitting.current = true; setBusy(true); setError('')
    // Disable replay immediately, including a browser timeout after provider acceptance.
    setPreview({ ...preview, can_send: false })
    try {
      const result = await request<{ notification_status: string }>(`${runPath(settings)}/${runId}/send`, {
        method: 'POST', body: JSON.stringify({ confirm: true, preview_hash: preview.preview_hash }),
      }, 75000)
      setPreview({ ...preview, can_send: false, notification_status: result.notification_status })
      setConfirmed(false); onChanged?.()
    } catch (e) { setError((e instanceof Error ? e.message : '发信结果未确认') + '。请重新预览核对状态，不自动重发。') }
    finally { submitting.current = false; setBusy(false) }
  }
  return <section className="manual-mail" aria-label="报告邮件预览">
    <button type="button" className="secondary-button" disabled={busy} onClick={() => void inspect()}>{busy ? '处理中…' : '预览邮件'}</button>
    {error && <p role="alert">{error}</p>}
    {preview && <div className="review-notes">
      <p>发件人：{preview.sender}<br/>收件人：{preview.recipient}<br/>主题：{preview.subject}</p>
      <details open><summary>邮件正文</summary><div className="report-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.body}</ReactMarkdown></div></details>
      <p role="status">{MAIL[preview.notification_status] || preview.notification_status}。仅按上方已审核的收件配置发送。</p>
      {preview.timings && <small>耗时：{Object.entries(preview.timings).map(([key,value]) => `${({ condition_seconds:'条件',report_seconds:'报告',total_seconds:'合计',duration_seconds:'合计',email_seconds:'邮件' } as Record<string,string>)[key] || key} ${value.toFixed(1)} 秒`).join(' · ')}。实际费用未知。</small>}
      {preview.can_send && <><label className="trial-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)}/>我已核对收件人与报告，确认发送这一封邮件</label>
        <button type="button" className="primary-button" disabled={!confirmed || busy} onClick={() => void send()}>确认发送一次</button></>}
    </div>}
  </section>
}

export function TaskTrial({ task, settings, request, onViewReports }: {
  task: Task; settings?: TrialSettings; request: Request; onViewReports: () => void
}) {
  const report = task.report as { condition_sql?: { bindings?: { business_date?: string }; local_test_only?: boolean } } | undefined
  const [open, setOpen] = useState(false)
  const [day, setDay] = useState(report?.condition_sql?.bindings?.business_date || '')
  const [confirmed, setConfirmed] = useState(false)
  const [run, setRun] = useState<Trial>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [reused, setReused] = useState(false)
  const [pollRevision, setPollRevision] = useState(0)
  const submitting = useRef(false)
  const running = !!run && !['completed','failed'].includes(run.phase)
  useEffect(() => {
    if (!open || !run?.run_id) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const value = await request<Trial>(`${runPath(settings)}/${run!.run_id}`, undefined, 15000)
        if (cancelled) return
        setRun(value); setError('')
        if (!['completed','failed'].includes(value.phase)) timer = setTimeout(() => void poll(), 2000)
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : '状态读取失败；可在分析报告中核对，不自动重新运行。') }
    }
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [open,run?.run_id,pollRevision,settings?.api])
  if (!settings?.enabled) return null
  async function start(retry = false) {
    if (!confirmed || !day || busy || running || submitting.current) return
    submitting.current = true; setBusy(true); setError('')
    try {
      const result = await request<{ run_id: string; reused: boolean }>(`/api/tasks/${encodeURIComponent(String(task.task_id))}/trial`, {
        method: 'POST', body: JSON.stringify({ business_date: day, expected_updated_at: task.updated_at,
          request_key: crypto.randomUUID(), confirm: true, ...(retry && run ? { retry_of: run.run_id } : {}) }),
      }, 30000)
      setReused(result.reused)
      setRun({ run_id: result.run_id, phase: 'queued', status: 'RUNNING', error: '', trigger_reason: '', report_text: '', timings: {}, notification_status: 'NOT_QUEUED' })
    } catch (e) { setError(e instanceof Error ? e.message : '无法提交试运行') }
    finally { submitting.current = false; setBusy(false) }
  }
  return <>
    <button type="button" className="secondary-button" disabled={!task.updated_at} onClick={() => setOpen(true)}>试运行一次</button>
    {open && <div className="modal-overlay"><div className="modal-content trial-modal" role="dialog" aria-modal="true" aria-label="手动试运行">
      <div className="modal-header"><h2>试运行：{String(task.name)}</h2><button type="button" className="icon-button" onClick={() => setOpen(false)} aria-label="关闭试运行">×</button></div>
      <div className="modal-body">
        <p>检查一次条件；条件成立时调用真实 Agent 生成报告。不会启动调度或自动发邮件。</p>
        <div className="form-field"><label htmlFor={`trial-date-${task.task_id}`}>试运行业务日期</label>
          <input id={`trial-date-${task.task_id}`} type="date" value={day} disabled={busy || running} onChange={e => { setDay(e.target.value); setRun(undefined); setConfirmed(false); setError('') }}/></div>
        {report?.condition_sql?.local_test_only && <p className="form-hint">临时模拟测试：选择 2025-06-24 或 2025-06-27；仅此测试任务比较上一个有数据日。</p>}
        <label className="trial-confirm"><input type="checkbox" checked={confirmed} disabled={busy || running} onChange={e => setConfirmed(e.target.checked)}/>确认查询数据，并在触发时调用真实 Agent；可能产生查询和推理费用</label>
        {error && <p role="alert">{error}</p>}
        {run && <section className="review-notes" aria-label="试运行进度">
          <strong>{PHASES[run.phase] || run.phase}{run.status === 'NOT_TRIGGERED' ? ' · 条件未满足，不调用报告 Agent' : ''}</strong>
          {reused && <p>已返回现有记录，没有重复发起模型调用。</p>}
          {run.trigger_reason && <p>{run.trigger_reason}</p>}
          {run.error && <p role="alert">{run.error}</p>}
          <p>{Object.entries(run.timings).map(([key,value]) => `${({ condition_seconds:'条件',report_seconds:'报告',total_seconds:'合计',email_seconds:'邮件' } as Record<string,string>)[key] || key} ${value.toFixed(1)} 秒`).join(' · ')}</p>
          {run.report_text && <><details open><summary>生成的报告</summary><div className="report-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{run.report_text}</ReactMarkdown></div></details>
            <ReportMail runId={run.run_id} settings={settings} request={request}/></>}
        </section>}
      </div>
      <div className="form-actions"><button type="button" className="secondary-button" onClick={() => { setOpen(false); onViewReports() }}>查看此任务的分析报告</button>
        {run && <button type="button" className="secondary-button" disabled={busy} onClick={() => setPollRevision(v => v + 1)}>刷新执行状态</button>}
        {run?.phase === 'failed' ? <button type="button" className="primary-button" disabled={!confirmed || busy} onClick={() => void start(true)}>明确重试这次失败的试运行</button>
          : <button type="button" className="primary-button" disabled={!day || !confirmed || busy || !!run} onClick={() => void start()}>{busy || running ? '处理中…' : run ? '本次试运行已完成' : '确认试运行一次'}</button>}
      </div>
      {!run && <p className="trial-button-hint">{!day ? '先选择业务日期。' : !confirmed ? '勾选上方确认框后，即可试运行。' : '试运行只检查条件和生成报告；不会发送邮件。'}</p>}
    </div></div>}
  </>
}
