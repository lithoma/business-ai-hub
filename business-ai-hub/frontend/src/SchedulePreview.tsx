import { useState } from 'react'
type Plan = { scheduler_enabled: boolean; scheduler?: {interval_seconds:number;state:string;ticks:number;last_tick?:{checked_at:string;state:string;duration_ms:number;error_type?:string}}; tasks: {task_id:string;name:string;active:boolean;due:boolean;already_claimed?:boolean;next_run_at?:string;timezone?:string;error?:string}[]; note:string }
type Request = <T>(url:string,options?:RequestInit,timeout?:number)=>Promise<T>
export function SchedulePreview({request}:{request:Request}) {
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[plan,setPlan]=useState<Plan>()
  async function inspect(){setOpen(true);setBusy(true);setError('');try{setPlan(await request<Plan>('/api/scheduler/plan'))}catch(e){setError(e instanceof Error?e.message:'计划读取失败')}finally{setBusy(false)}}
  return <><button className="secondary-button" onClick={()=>void inspect()}>调度计划</button>{open&&<div className="modal-overlay"><div className="modal-content" role="dialog" aria-modal="true" aria-label="调度计划">
    <header className="modal-header"><h2>调度计划 · {plan?.scheduler_enabled?'定时检查已开启':'自动运行未开启'}</h2><button className="icon-button" onClick={()=>setOpen(false)} aria-label="关闭调度计划">×</button></header>
    <p className="form-hint">这里只预览各任务的时间，不执行 SQL、调用模型或发邮件。</p>
    {plan?.scheduler&&<p className="form-hint">执行器每 {plan.scheduler.interval_seconds} 秒检查一次 · {plan.scheduler_enabled?'运行中':'当前关闭'}；业务任务频率仍为每日、每周或间隔天数。
      {plan.scheduler.last_tick&&<> 最近检查：{plan.scheduler.last_tick.checked_at} · {plan.scheduler.last_tick.state==='failed'?`失败（${plan.scheduler.last_tick.error_type}）`:'完成'} · {plan.scheduler.last_tick.duration_ms} 毫秒</>}
    </p>}
    {busy&&<p role="status">正在读取计划…</p>}{error&&<p role="alert">{error}</p>}
    {plan&&<div className="schedule-plan-list">{plan.tasks.map(task=><article key={task.task_id}><strong>{task.name}</strong><span>{task.active?'已审核启用':'草稿或暂停'}</span><p>下一次计划：{task.next_run_at?.replace('T',' ')||'未确定'}{task.timezone?` · ${task.timezone}`:''}</p><p>{task.error||(!task.active?'尚未进入执行队列':task.already_claimed?'今天的计划已有运行记录，不重复执行':task.due?'今天已到计划时间；需要获准的执行器检查':'等待计划时间')}</p></article>)}</div>}
    <div className="form-actions"><button className="secondary-button" disabled={busy} onClick={()=>void inspect()}>刷新计划</button><button className="primary-button" onClick={()=>setOpen(false)}>知道了</button></div>
  </div></div>}</>
}
