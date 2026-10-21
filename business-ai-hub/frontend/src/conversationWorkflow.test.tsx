// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import { EMPTY_FORM, buildCreatePayload } from './taskDrafts'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('unified conversation (offline fixtures)', () => {
  it('reviews and activates a saved task inside chat, then opens its filtered report history', async () => {
    const fixture = buildCreatePayload({ ...EMPTY_FORM, task_id: 'fixture-inline', name: '对话内启用测试' })
    let stored: Record<string, unknown> | undefined
    const calls: { url: string; init?: RequestInit }[] = []
    HTMLElement.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init })
      let value: unknown = {}
      if (url === '/api/conversation/settings') value = { enabled: true, analysis_provider: 'legacy' }
      if (url === '/api/conversation/route') value = { intent: 'task', execution_order: 'independent', task_requirements: '每周报告', configuration: fixture }
      if (url === '/api/tasks/create') stored = { ...JSON.parse(String(init?.body)), _source: 'registry', lifecycle_status: 'draft', updated_at: 'version1' }
      if (url === '/api/tasks') value = { tasks: stored ? [stored] : [] }
      if (url === '/api/tasks/fixture-inline' && init?.method === 'PUT') stored = { ...stored, ...JSON.parse(String(init.body)), updated_at: 'version2' }
      if (url.endsWith('/readiness')) value = { errors: [], warnings: [], can_activate: true, execution_enabled: true, updated_at: 'version2', configuration: stored }
      if (url.endsWith('/lifecycle')) { stored = { ...stored, enabled: true, lifecycle_status: 'active', updated_at: 'version3' }; value = stored }
      if (url.startsWith('/api/reports?')) value = { runs: [], next_offset: null }
      return { ok: true, status: 200, text: async () => JSON.stringify(value) }
    }))
    render(<App />)
    const user = userEvent.setup()
    await screen.findByText('统一对话 · 分析与任务草稿')
    await user.type(screen.getByLabelText('输入业务问题'), '每周给我报告')
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByText('对话内启用测试')
    await user.click(screen.getByLabelText('我已核对配置；保存为草稿，不会启用或发送报告'))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await screen.findByRole('button', { name: '审核启用' })
    expect(calls.some(call => call.url.endsWith('/lifecycle'))).toBe(false)
    // Editing the saved card updates the original task instead of creating it again.
    await user.click(screen.getByRole('button', { name: '编辑草稿' }))
    await user.clear(screen.getByLabelText('任务名称 *'))
    await user.type(screen.getByLabelText('任务名称 *'), '已修改的对话任务')
    await user.click(screen.getByRole('button', { name: '预览并保存' }))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await screen.findByRole('button', { name: '审核启用' })
    expect(calls.filter(call => call.url === '/api/tasks/create')).toHaveLength(1)
    expect(calls.filter(call => call.url === '/api/tasks/fixture-inline' && call.init?.method === 'PUT')).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: '审核启用' }))
    const dialog = within(await screen.findByRole('dialog', { name: '审核启用' }))
    const confirm = dialog.getByRole('button', { name: '确认审核启用' })
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    await user.click(dialog.getByRole('checkbox'))
    await user.click(confirm)
    await screen.findByRole('button', { name: '暂停任务' })
    expect(screen.getByRole('log', { name: '对话记录' })).toBeTruthy()
    const activated = calls.filter(call => call.url.endsWith('/lifecycle'))
    expect(activated).toHaveLength(1)
    expect(JSON.parse(String(activated[0].init?.body))).toEqual({ action: 'activate', confirm: true, reviewed: true, expected_updated_at: 'version2' })
    await user.click(screen.getByRole('button', { name: '查看此任务的分析报告' }))
    await screen.findByText('暂无执行记录。保存草稿不会生成报告。')
    expect((screen.getByLabelText('任务 ID') as HTMLInputElement).value).toBe('fixture-inline')
    expect(calls.find(call => call.url.startsWith('/api/reports?'))?.url).toContain('task_id=fixture-inline')
    expect(calls.some(call => /jobs|deploy|email|\/generate|\/test$/.test(call.url))).toBe(false)
  })

  it('generates trigger SQL alongside analysis, preserves failed drafts and retries SQL without reparsing', async () => {
    const calls: { url: string; init?: RequestInit }[] = []
    const fixture = buildCreatePayload({ ...EMPTY_FORM, task_id: 'fixture-sql', name: '离线量化监控',
      trigger_type: 'sql', trigger_prompt: 'GMV比前一日下降超过10%', trigger_agent: 'genie_a2',
      condition_agents: ['genie_a2','genie_a1'] })
    let finishSQL: () => void = () => {}
    const pendingSQL = new Promise<void>(resolve => { finishSQL = resolve })
    let attempts = 0
    HTMLElement.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init })
      let value: unknown = { tasks: [] }
      let status = 200
      if (url === '/api/conversation/settings') value = { enabled: true, analysis_provider: 'legacy' }
      if (url === '/api/conversation/route') value = { intent: 'mixed', execution_order: 'independent',
        task_requirements: '每天监控GMV下降10%', analysis_agent: 'genie_a1', analysis_ticket: 'fixture-ticket', configuration: fixture }
      if (url === '/api/conversation/analysis') value = { answer: 'fixture：独立分析已完成', provider: 'legacy' }
      if (url === '/api/conditions/sql/generate') {
        attempts++
        if (attempts === 1) {
          await pendingSQL
          value = { detail: 'fixture：需明确指标口径' }; status = 422
        } else value = { sql: 'SELECT true AS should_trigger', review: { window: 'day' }, warnings: [] }
      }
      return { ok: status < 400, status, text: async () => JSON.stringify(value) }
    }))
    render(<App />)
    const user = userEvent.setup()
    await screen.findByText('统一对话 · 分析与任务草稿')
    await user.type(screen.getByLabelText('输入业务问题'), '分析销售，同时每天监控GMV下降10%')
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByText('fixture：独立分析已完成')
    const flow = within(screen.getByRole('complementary', { name: '分流与执行流程' }))
    expect(flow.getByLabelText('Genie 生成触发 SQL：进行中')).toBeTruthy()
    const sqlRequest = calls.find(call => call.url === '/api/conditions/sql/generate')!
    expect(JSON.parse(String(sqlRequest.init?.body))).toMatchObject({ agent: 'genie_a2', context_agents: ['genie_a2','genie_a1'], window: 'day' })
    expect(sqlRequest.init?.headers).toEqual(calls.find(call => call.url === '/api/conversation/route')?.init?.headers)
    finishSQL()
    await screen.findByRole('alert')
    expect(screen.getByText('离线量化监控')).toBeTruthy()
    expect(flow.getByLabelText('Genie 生成触发 SQL：失败')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '仅重试生成 SQL' }))
    await waitFor(() => expect(flow.getByLabelText('Genie 生成触发 SQL：完成')).toBeTruthy())
    expect(screen.getByText('SELECT true AS should_trigger')).toBeTruthy()
    expect(calls.filter(call => call.url === '/api/conditions/sql/generate')).toHaveLength(2)
    expect(calls.filter(call => call.url === '/api/conversation/analysis')).toHaveLength(1)
    expect(calls.some(call => /\/tasks\/generate|\/create|\/test$/.test(call.url))).toBe(false)
  })

  it('shows a draft before analysis finishes and retains it on failure, with no second parser call', async () => {
    const calls: string[] = []
    const fixture = buildCreatePayload({ ...EMPTY_FORM, task_id: 'fixture', name: '仅离线测试周报', schedule_freq: 'weekly' })
    let finishAnalysis: (value: unknown) => void = () => {}
    const pending = new Promise(resolve => { finishAnalysis = resolve })
    HTMLElement.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url)
      if (url === '/api/conversation/analysis') await pending
      const value = url === '/api/conversation/settings' ? { enabled: true, analysis_provider: 'legacy', metrics_enabled: true }
        : url === '/api/conversation/route' ? { intent: 'mixed', execution_order: 'independent',
          task_requirements: '每周五生成周报', analysis_agent: 'genie_a1', analysis_ticket: 'fixture-ticket', configuration: fixture }
        : url === '/api/conversation/analysis' ? { detail: 'fixture：分析接口暂不可用' }
        : { status: 'created', tasks: [] }
      return { ok: url !== '/api/conversation/analysis', status: url === '/api/conversation/analysis' ? 503 : 200,
        text: async () => JSON.stringify(value) }
    }))
    render(<App />)
    const user = userEvent.setup()
    await screen.findByText('统一对话 · 分析与任务草稿')
    expect(screen.getByRole('link', { name: '导出全部测试数据（JSON）' }).getAttribute('href')).toBe('/api/test-metrics/export')
    await user.type(screen.getByLabelText('输入业务问题'), '先分析善存销售，同时每周五给我报告')
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByText('仅离线测试周报')
    const flow = within(screen.getByRole('complementary', { name: '分流与执行流程' }))
    expect(flow.getByLabelText('模型分流＋任务信息提取：完成')).toBeTruthy()
    expect(flow.getByLabelText('A1 销售运营：进行中')).toBeTruthy()
    expect(flow.getByLabelText('可编辑任务草稿：完成')).toBeTruthy()
    expect(calls.filter(url => url === '/api/conversation/route')).toHaveLength(1)
    expect(calls.some(url => /\/generate|\/create/.test(url))).toBe(false)
    finishAnalysis(null)
    await screen.findAllByText('fixture：分析接口暂不可用')
    expect(flow.getByLabelText('A1 销售运营：失败')).toBeTruthy()
    expect(screen.getByText('仅离线测试周报')).toBeTruthy()
    await user.click(screen.getByLabelText('我已核对配置；保存为草稿，不会启用或发送报告'))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await waitFor(() => expect(calls.filter(url => url === '/api/tasks/create')).toHaveLength(1))
    await waitFor(() => expect(flow.getByLabelText('审核并保存：完成')).toBeTruthy())
    expect(calls.filter(url => url === '/api/conversation/analysis')).toHaveLength(1)
  })

  it('defers dependent draft generation until analysis is available and carries the answer', async () => {
    const calls: string[] = []
    HTMLElement.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url)
      const value = url === '/api/conversation/settings' ? { enabled: true, analysis_provider: 'legacy' }
        : url === '/api/conversation/route' ? { intent: 'mixed', execution_order: 'analysis_first',
          task_requirements: '根据分析设置阈值并每周检查', analysis_agent: 'genie_a1', analysis_ticket: 'fixture-ticket' }
        : { answer: 'fixture：需要业务确认阈值', conversation_id: 'fixture-c', provider: 'legacy' }
      return { ok: true, status: 200, text: async () => JSON.stringify(value) }
    }))
    render(<App />)
    const user = userEvent.setup()
    await screen.findByText('统一对话 · 分析与任务草稿')
    await user.type(screen.getByLabelText('输入业务问题'), '先分析销售，再根据结果设置每周任务的阈值')
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByRole('region', { name: '对话任务草稿' })
    expect(screen.getByText('带入的上下文：2 条 · 销售与运营 A1')).toBeTruthy()
    expect(screen.getByRole('button', { name: '生成任务配置' })).toBeTruthy()
    expect(calls.some(url => /\/generate|\/create/.test(url))).toBe(false)
  })

  it('keeps AI anomaly conditions as prompts and offers history-first rule clarification', async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = []
    const fixture = buildCreatePayload({ ...EMPTY_FORM, task_id: 'fixture-anomaly', name: '异常判断草稿',
      trigger_type: 'agent', trigger_prompt: '依据已有异常规则判断；没有规则时要求澄清' })
    let routes = 0
    HTMLElement.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : {} })
      const value = url === '/api/conversation/settings' ? { enabled: true, analysis_provider: 'legacy' }
        : url === '/api/conversation/route' ? ++routes === 1
          ? { intent: 'task', execution_order: 'independent', task_requirements: '每天善存销售异常时报告', configuration: fixture }
          : { intent: 'mixed', execution_order: 'analysis_first', task_requirements: '确认异常规则后设置任务', analysis_ticket: 'fixture-ticket' }
        : { answer: 'fixture：历史基线需要确认', provider: 'legacy' }
      return { ok: true, status: 200, text: async () => JSON.stringify(value) }
    }))
    render(<App />)
    const user = userEvent.setup()
    await screen.findByText('统一对话 · 分析与任务草稿')
    await user.type(screen.getByLabelText('输入业务问题'), '每天善存销售异常时报告')
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByText('AI 定性判断', { exact: false })
    const analyze = screen.getByRole('button', { name: '先分析历史并确认规则' })
    await waitFor(() => expect((analyze as HTMLButtonElement).disabled).toBe(false))
    expect(calls.some(call => /\/conditions\/|\/analysis$/.test(call.url))).toBe(false)
    await user.click(analyze)
    await screen.findAllByText('fixture：历史基线需要确认')
    expect(calls.filter(call => call.url === '/api/conversation/analysis')).toHaveLength(1)
    expect(calls.filter(call => call.url === '/api/conversation/route')[1].body.requirements).toContain('不要自行决定阈值')
    expect(calls.some(call => /\/conditions\/|\/tasks\/generate|\/create/.test(call.url))).toBe(false)
  })
})
