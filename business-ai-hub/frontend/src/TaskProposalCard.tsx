import { useEffect, useState, type ReactNode } from 'react'
import { scheduleDescription } from './taskDrafts'
import type { TaskSeed } from './taskConversation'

export function TaskProposalCard({ task, busy, saving, sqlBusy, analysisBusy, savedActions, onRequirements, onGenerate, onGenerateSQL, onAnalyzeRule, onEdit, onSave, onViewTasks }: {
  task: TaskSeed; busy: boolean; saving: boolean; sqlBusy: boolean; analysisBusy: boolean
  savedActions?: ReactNode
  onRequirements: (text: string) => void; onGenerate: () => void; onGenerateSQL: () => void; onAnalyzeRule: () => void; onEdit: () => void; onSave: () => void; onViewTasks: () => void
}) {
  const config = task.configuration
  const [reviewed,setReviewed] = useState(false)
  useEffect(() => setReviewed(false),[config])
  return <section className="conversation-task" aria-label="对话任务草稿">
    <strong>{task.saved ? '已保存的任务' : '将讨论设为自动任务'}</strong>
    <details className="conversation-context"><summary>带入的上下文：{task.conversation.length} 条 · Genie One</summary>
      {task.conversation.length ? task.conversation.map((turn,index) => <div key={index}><strong>{turn.role === 'user' ? '你' : 'Genie'}</strong><p>{turn.content}</p></div>) : <p>没有前文，仅使用本次任务要求。</p>}
    </details>
    {!task.saved && <label>任务要求（可补充频率与条件）<textarea aria-label="对话中的任务要求" value={task.requirements} maxLength={4000} disabled={busy || saving} onChange={event => onRequirements(event.target.value)}/></label>}
    {config && <div className="conversation-task-summary">
      <h3>{config.name}</h3>
      <p>{scheduleDescription(config.schedule)}</p>
      <p>触发：{config.trigger.type === 'always' ? '每次检查都生成报告' : config.trigger.type === 'sql' ? 'SQL 条件' : config.trigger.type === 'agent' ? 'AI 定性判断' : '待补充条件'}</p>
      {config.trigger.prompt && <p>{config.trigger.prompt}</p>}
      <p>业务知识来源：Genie One；保存的 SQL 直接执行，不重复生成。</p>
      <p>报告：{config.report.prompt || '待补充'}</p>
      {config.trigger.type === 'sql' && <details open={!config.trigger.sql}><summary>{config.trigger.sql ? '查看生成的 SQL（待审核和测试）' : sqlBusy ? 'Genie 正在生成触发 SQL…' : 'SQL 尚未生成，可单独重试'}</summary><pre>{config.trigger.sql || '尚未生成'}</pre></details>}
      {task.sqlError && <p role="alert">{task.sqlError}</p>}
      {!!config.report.draft_review?.missing_information.length && <div><strong>待确认</strong><ul>{config.report.draft_review.missing_information.map((item,index) => <li key={index}>{item}</li>)}</ul></div>}
      {!!config.report.draft_review && <details><summary>生成假设与说明</summary><ul>{[...config.report.draft_review.assumptions,...config.report.draft_review.warnings].map((item,index) => <li key={index}>{item}</li>)}</ul></details>}
    </div>}
    {!task.saved ? <>
      {config && <label className="conversation-task-review"><input type="checkbox" checked={reviewed} disabled={busy || saving} onChange={event => setReviewed(event.target.checked)}/>我已核对配置；保存为草稿，不会启用或发送报告</label>}
      <div className="heading-actions">
        <button className="secondary-button" disabled={busy || saving || !task.requirements.trim()} onClick={onGenerate}>{busy ? sqlBusy ? '正在生成 SQL…' : '正在生成…' : config ? '重新生成配置' : '生成任务配置'}</button>
        {config?.trigger.type === 'sql' && !config.trigger.sql && <button className="secondary-button" disabled={busy || saving} onClick={onGenerateSQL}>仅重试生成 SQL</button>}
        {config && (config.trigger.type === 'agent' || config.trigger.type === 'unconfigured') && <button className="secondary-button" disabled={busy || saving || analysisBusy} onClick={onAnalyzeRule}>先分析历史并确认规则</button>}
        {config && <><button className="secondary-button" disabled={busy || saving} onClick={onEdit}>审核并编辑</button><button className="primary-button" disabled={busy || saving || !reviewed} onClick={onSave}>{saving ? '保存中…' : '确认保存为草稿'}</button></>}
      </div><small>点击生成会调用真实模型；SQL 条件还会使用所需 Genie 查询业务上下文。生成不保存，保存不启动任务。</small>
    </> : <div className="heading-actions">{savedActions}<button className="secondary-button" onClick={onViewTasks}>查看我的任务</button></div>}
  </section>
}
