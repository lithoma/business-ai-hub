import { useEffect, useRef } from 'react'
import { Check, ClipboardList, ShieldCheck, X } from 'lucide-react'

const GROUPS = [
  { title: '对话与任务创建', items: ['业务提问与多轮对话：统一使用 Genie One', '自动分流：分析、任务、混合请求或需要澄清', '支持先分析，再由你确认规则并生成任务', '自然语言生成可编辑草稿，也可手动创建', '在对话卡片中编辑、保存、审核及查看任务报告'] },
  { title: '条件与任务管理', items: ['量化条件优先 SQL；定性条件支持 AI 判断', 'Genie One 生成 SQL，保留来源与审核信息', '指定业务日期，运行一次只读条件测试', '审核保存的 SQL 按日期参数重复执行', '草稿编辑、删除、审核启用、暂停及恢复编辑', '每日、每周、间隔计划；可预览每个任务的下一次计划时间'] },
  { title: '报告与手动邮件', items: ['明确点击后试运行一次；条件成立才生成报告', '条件不成立或无法判断时不发报告邮件', '报告历史、任务筛选、状态与错误展示', '预览邮件，再明确确认发送到固定测试邮箱', '重复请求去重；发信结果不确定时不自动重发'] },
  { title: '可观察性与迁移', items: ['竖向查看消息分流、SQL 生成、保存的进度', '记录各阶段耗时、已返回的 tokens 和调用信息', '导出测试记录；未知费用不伪装成已统计', '本地和公司两套公开配置，缺项明确阻止公司启动', '公司接入 ZIP、管理员说明和待审核建表/授权模板'] },
]

export function FeatureOverview({ onClose, shared, apps, obo, trialsEnabled, mailAvailable }: {
  onClose: () => void; shared: boolean; apps?: boolean; obo?: boolean; trialsEnabled: boolean; mailAvailable: boolean
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal(); return () => dialog.current?.close() }, [])
  return <dialog className="feature-overview" ref={dialog} onCancel={onClose} aria-labelledby="feature-overview-title">
    <header><div><ClipboardList size={20}/><h2 id="feature-overview-title">当前版本能做什么</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭功能说明"><X size={20}/></button></header>
    <p className="feature-lead">{obo ? '公司 App：按登录用户的数据权限查询，任务与报告绑定创建者。' : apps ? '公司 App：平台登录，业务调用使用 App 共享身份，任务和报告保存在配置的持久表中。' : shared ? '共享试用：所有访客共用开发者测试身份和本地任务库。' : '本地测试：使用当前开发者身份，任务保存在本机。'}真实提问、SQL 和报告会调用 Databricks；审核启用不等于后台已经在运行。</p>
    <div className="feature-live"><span><Check size={14}/>交互提问与任务草稿</span><span>{trialsEnabled ? '手动试运行入口已开启' : '手动试运行入口未开启'}</span><span>{mailAvailable ? '固定测试邮箱 · 逐次确认发信' : '邮件通道不可用'}</span></div>
    <div className="feature-groups">{GROUPS.map(group => <section key={group.title}><h3>{group.title}</h3><ul>{group.items.map(item => <li key={item}>{item}</li>)}</ul></section>)}</div>
    <section className="feature-pending"><h3><ShieldCheck size={16}/>仍待完成的公司接入</h3><p>公司 Databricks Apps 部署与身份接入、创建者长期后台授权、定时器启用和公司邮件授权。试运行与发信 API 已独立于本地启动脚本，调度核心已实现，尚未开启后台定时运行。共享试用暂时关闭访客身份隔离；权限控制代码保留。</p><p>CSV 导出适配、公司业务模板与指标口径之后接入。历史邮件实测发生在统一 Genie One 之前，切换后的完整报告链仍需用户复验。Genie 内部 token 和真实费用未返回时仍记为未知。</p></section>
  </dialog>
}
