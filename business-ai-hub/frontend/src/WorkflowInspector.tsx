import { Check, Circle, GitBranch, LoaderCircle, LockKeyhole, X } from 'lucide-react'

export type StepState = 'waiting' | 'running' | 'done' | 'error' | 'skipped' | 'blocked'
export type WorkflowTrace = {
  id: string; request: string; intent?: 'analysis' | 'task' | 'mixed' | 'clarify'
  order?: 'independent' | 'analysis_first'; routing: StepState; analysis: StepState
  proposal: StepState; save: StepState; provider: string; analysisAgent?: string
  analysisQuestion?: string; taskRequirements?: string; taskAgents?: string[]
  routingMs?: number; analysisMs?: number; tokens?: number; error?: string
  sql?: StepState; sqlMs?: number; sqlError?: string
}

const STATES: Record<StepState, string> = {
  waiting: '等待', running: '进行中', done: '完成', error: '失败', skipped: '未走此路', blocked: '未启动',
}
const INTENTS = { analysis: '只做分析', task: '设置任务', mixed: '分析＋任务', clarify: '需要澄清' }
const agentName = (_agent: string) => 'Genie One'
const duration = (value?: number) => value === undefined ? '' : ` · ${(value / 1000).toFixed(1)} 秒`

function Node({ title, state, detail }: { title: string; state: StepState; detail?: string }) {
  const Icon = state === 'done' ? Check : state === 'running' ? LoaderCircle : state === 'error' ? X : state === 'blocked' ? LockKeyhole : Circle
  return <div className={`flow-node flow-${state}`} aria-label={`${title}：${STATES[state]}`}>
    <span className="flow-node-icon"><Icon size={14} /></span>
    <div><strong>{title}</strong><span className="flow-state">{STATES[state]}</span>{detail && <p>{detail}</p>}</div>
  </div>
}

export function WorkflowInspector({ traces, selected, onSelect, provider, model, metricsEnabled }: {
  traces: WorkflowTrace[]; selected?: string; onSelect: (id: string) => void; provider: string; model?: string; metricsEnabled?: boolean
}) {
  const trace = traces.find(item => item.id === selected)
  const analysisName = (trace?.provider || provider) === 'genie_one' ? 'Genie One'
    : trace?.analysisAgent ? agentName(trace.analysisAgent) : 'Genie One'
  return <aside className="workflow-inspector" aria-label="分流与执行流程">
    <header><div><GitBranch size={17}/><strong>分流与执行流程</strong></div><span>本地测试</span></header>
    <p className="flow-intro">{trace ? '本条消息的实际处理路径' : '发送消息后，高亮实际分流与执行进度'}</p>
    {traces.length > 0 && <label className="flow-select">查看哪条消息
      <select aria-label="选择消息查看分流" value={trace?.id || ''} onChange={event => onSelect(event.target.value)}>
        {[...traces].reverse().map((item, index) => <option key={item.id} value={item.id}>{index === 0 ? '最新 · ' : ''}{item.request.slice(0,45)}</option>)}
      </select>
    </label>}
    <div className="flow-diagram" role="group" aria-label="竖向处理流程">
      <Node title="收到你的消息" state={trace ? 'done' : 'waiting'} detail={trace?.request || '输入业务问题，或描述需要设置的任务'}/>
      <div className="flow-connector"/>
      <Node title="模型分流＋任务信息提取" state={trace?.routing || 'waiting'}
        detail={trace?.intent ? `${INTENTS[trace.intent]}${duration(trace.routingMs)}${trace.tokens === undefined ? '' : ` · ${trace.tokens} tokens`}` : model || '一次模型调用，决定后续路径'}/>
      {trace?.intent === 'clarify' && <p className="flow-clarify">等待你补充信息；本轮没有提交业务分析或创建任务。</p>}
      <div className="flow-fork"><span>分析分支</span><span>任务分支</span></div>
      <div className="flow-branches">
        <div><Node title={analysisName} state={trace?.analysis || 'waiting'}
          detail={trace?.analysis === 'running' ? '已提交分析请求，等待接口返回' : trace?.analysis === 'done' ? `已收到真实接口回答${duration(trace.analysisMs)}` : '一次性业务分析'}/>
          <p className="flow-branch-note">Genie 内部选了哪些资产，需要其返回证据；这里不推测。</p>
        </div>
        <div><Node title="可编辑任务草稿" state={trace?.proposal || 'waiting'}
          detail={trace?.order === 'analysis_first' && trace.proposal === 'waiting' ? '依赖分析结果，之后点击生成' : '整理配置，不自动保存'}/>
          <div className="flow-connector"/>
          <Node title="Genie 生成触发 SQL" state={trace?.sql || 'waiting'}
            detail={trace?.sql === 'running' ? '使用所选数据域的指标口径与表结构' : trace?.sql === 'done' ? `SQL 已填入草稿，待审核和单独测试${duration(trace.sqlMs)}` : trace?.sql === 'error' ? `${trace.sqlError || '可单独重试，不重新解析任务'}${duration(trace.sqlMs)}` : trace?.sql === 'skipped' ? '当前不需要生成 SQL' : '量化条件生成并保存 SQL；以后按日期参数重复执行'}/>
          <div className="flow-connector"/>
          <Node title="审核并保存" state={trace?.save || 'waiting'} detail="你确认后，保存到当前用户的任务库"/>
        </div>
      </div>
      {trace?.intent === 'mixed' && <p className="flow-order">{trace.order === 'analysis_first' ? '顺序：先分析，再由你生成任务配置。' : '独立处理：草稿先展示，分析继续等待。'}</p>}
      <details className="flow-payload"><summary>查看实际交给各分支的内容</summary>
        <strong>分析问题 → {analysisName}</strong><p>{trace?.analysisQuestion || '本轮未提交分析问题'}</p>
        <strong>任务要求 → Hub</strong><p>{trace?.taskRequirements || '本轮没有任务要求'}</p>
        <strong>任务内的业务知识来源</strong><p>{trace?.taskAgents?.length ? [...new Set(trace.taskAgents)].map(agentName).join('、') : 'Genie One 统一选择业务资产'}</p>
      </details>
      {trace?.error && <p className="flow-error" role="status">{trace.error}</p>}
      <div className="flow-connector"/>
      <Node title="后台调度 → 报告 → 发邮件" state="blocked" detail="保存的 SQL 直接执行 → 条件成立才生成报告；定性条件与报告使用 Genie One。"/>
    </div>
    <footer>{metricsEnabled ? <>耗时与调用记录自动保存在本机，无需 Codex 在线监控。页面流程刷新后清空，后台记录保留。<br/>
      <a href="/api/test-metrics/export" download>导出全部测试数据（JSON）</a><br/>Genie 内部用量和实际费用未返回时记为未知。</>
      : '状态来自接口响应；刷新页面会清空流程记录，已保存的任务保留。'}</footer>
  </aside>
}
