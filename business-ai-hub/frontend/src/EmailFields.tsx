import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'
import type { TaskFormState } from './taskDrafts'
type Request = <T>(url: string, options?: RequestInit, timeoutMs?: number) => Promise<T>
type Options = { templates: { id: string; label: string }[]; csv_exports: { id: string; label: string }[]; email_enabled: boolean }

export function EmailFields({ form, setForm, request }: { form: TaskFormState; setForm: Dispatch<SetStateAction<TaskFormState>>; request: Request }) {
  const [options, setOptions] = useState<Options | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!form.email_enabled) return
    let active = true
    setOptions(null); setError('')
    request<Options>('/api/report-options', undefined, 60000).then(data => {
      if (!Array.isArray(data.templates) || !Array.isArray(data.csv_exports)) throw new Error('邮件配置目录无效。')
      if (active) setOptions(data)
    }).catch(e => { if (active) setError(e instanceof Error ? e.message : '无法读取邮件配置目录。') })
    return () => { active = false }
  }, [form.email_enabled, request])
  return <section className="email-fields" aria-label="邮件与数据附件设置">
    <label><input type="checkbox" checked={form.email_enabled} onChange={e => setForm(f => ({ ...f, email_enabled: e.target.checked }))}/> 生成报告后发送邮件</label>
    {form.email_enabled && <>
      <p className="form-hint">仅保存通知设置，不会立即发送。收件人须由你确认，CSV 数据集由团队接入。</p>
      {error && <p role="alert">{error}</p>}
      {options && !options.email_enabled && <p>项目邮件通道尚未配置，可先保存草稿。</p>}
      <div className="form-field"><label htmlFor="email-to">收件人邮箱 *</label><input id="email-to" value={form.email_to} onChange={e => setForm(f => ({ ...f, email_to: e.target.value }))} placeholder="多个邮箱用逗号分隔"/></div>
      <div className="form-field"><label htmlFor="email-cc">抄送邮箱</label><input id="email-cc" value={form.email_cc} onChange={e => setForm(f => ({ ...f, email_cc: e.target.value }))}/></div>
      <div className="form-field"><label htmlFor="email-template">邮件模板</label><select id="email-template" value={form.email_template} onChange={e => setForm(f => ({ ...f, email_template: e.target.value }))}>
        {!options?.templates.some(item => item.id === form.email_template) && <option value={form.email_template}>{form.email_template}（待核对）</option>}
        {options?.templates.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></div>
      <div className="form-field"><label htmlFor="email-subject">邮件主题（可选）</label><input id="email-subject" value={form.email_subject} onChange={e => setForm(f => ({ ...f, email_subject: e.target.value }))} placeholder="留空使用任务名称与执行日期"/></div>
      <div><strong>CSV 附件</strong>
        {options?.csv_exports.length === 0 && <p className="form-hint">暂无已接入的数据集。团队提供数据查询后，可在这里选择。</p>}
        {options?.csv_exports.map(item => <label className="export-choice" key={item.id}><input type="checkbox" checked={form.csv_exports.includes(item.id)} onChange={e => setForm(f => ({ ...f, csv_exports: e.target.checked ? [...f.csv_exports, item.id] : f.csv_exports.filter(key => key !== item.id) }))}/>{item.label}</label>)}
        {form.csv_exports.filter(key => !options?.csv_exports.some(item => item.id === key)).map(key => <p key={key}>待核对的数据集：{key} <button type="button" onClick={() => setForm(f => ({ ...f, csv_exports: f.csv_exports.filter(id => id !== key) }))}>移除</button></p>)}
      </div>
    </>}
  </section>
}
