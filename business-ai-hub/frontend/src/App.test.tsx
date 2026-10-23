// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import { EMPTY_FORM, buildCreatePayload } from './taskDrafts'

function generatedFixture() {
  return buildCreatePayload({
    ...EMPTY_FORM, task_id: 'fixture-draft', name: 'AI 测试配置', schedule_freq: 'weekly',
    trigger_type: 'agent', trigger_prompt: '善存上一周 GMV 比前一周下降超过15%',
    report_prompt: '分析下降原因',
    draft_review: { original_request: '如果下降15%', conditional: true,
      assumptions: ['建议北京时间09:00'], warnings: ['仅草稿'], missing_information: ['确认GMV口径'] },
  })
}

let calls: { url: string; body: Record<string, unknown> }[]
let generateResponse: { status: number; body: unknown }
let sqlGenerateResponse: { status: number; body: unknown }
let saveStatus: number
let createdTask: Record<string, unknown> | null

beforeEach(() => {
  calls = []
  generateResponse = { status: 200, body: { configuration: generatedFixture() } }
  sqlGenerateResponse = { status: 200, body: { sql: 'SELECT false AS should_trigger', review: { window: 'day_vs_week' }, warnings: [] } }
  saveStatus = 200
  createdTask = null
  HTMLElement.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {}
    calls.push({ url, body })
    if (url === '/api/tasks/create' && saveStatus === 200) createdTask = { ...body, _source: 'registry', lifecycle_status: 'draft' }
    const result = url === '/api/tasks/generate' ? generateResponse
      : url === '/api/chat' ? { status: 200, body: { answer: 'fixture：讨论善存的GMV与周度变化。', conversation_id: 'fixture-c' } }
      : url === '/api/conditions/sql/generate' ? sqlGenerateResponse
      : url === '/api/tasks/create' ? { status: saveStatus, body: saveStatus === 200 ? { status: 'created' } : { detail: '保存失败，测试错误' } }
      : { status: 200, body: { tasks: [
        { task_id: 'enabled', name: 'YAML日报', enabled: true, _source: 'yaml' },
        { task_id: 'draft', name: '历史草稿', enabled: true, _source: 'registry', lifecycle_status: 'draft' },
        ...(createdTask ? [createdTask] : []),
      ] } }
    return { ok: result.status < 400, status: result.status, text: async () => JSON.stringify(result.body) }
  }))
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function openForm() {
  render(<App/>)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: '我的任务' }))
  await screen.findByText('历史草稿')
  await user.click(screen.getByRole('button', { name: '创建任务' }))
  return user
}

describe('natural language draft workflow (mocked transport only)', () => {
  it('continues SQL task generation with the correct Genie sources and comparison window', async () => {
    const fixture = generatedFixture()
    fixture.trigger = { type: 'sql', sql: '', agent: 'genie_a2', context_agents: ['genie_a2','genie_a1'], prompt: '检查前一日指标是否高于上周平均值' }
    generateResponse = { status: 200, body: { configuration: fixture } }
    const user = await openForm()
    await user.type(screen.getByLabelText('用自然语言描述任务'), '每日比较指定指标')
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    await waitFor(() => expect((screen.getByLabelText('触发 SQL 条件（待审核）') as HTMLTextAreaElement).value).toBe('SELECT false AS should_trigger'))
    expect(calls.filter(c => c.url === '/api/tasks/generate')).toHaveLength(1)
    const sqlCall = calls.filter(c => c.url === '/api/conditions/sql/generate')
    expect(sqlCall).toHaveLength(1)
    expect(sqlCall[0].body).toMatchObject({ agent: 'genie_a2', context_agents: ['genie_a2','genie_a1'], window: 'day_vs_week', original_request: '如果下降15%' })
    expect(calls.some(c => /\/test$|\/create$|lifecycle/.test(c.url))).toBe(false)
  })

  it('keeps the parsed task after SQL generation fails and allows SQL-only retry', async () => {
    const fixture = generatedFixture()
    fixture.trigger.type = 'sql'
    generateResponse = { status: 200, body: { configuration: fixture } }
    sqlGenerateResponse = { status: 422, body: { detail: 'Genie 缺少已核对的指标口径' } }
    const user = await openForm()
    await user.type(screen.getByLabelText('用自然语言描述任务'), '每日比较指定指标')
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    await screen.findByText(/任务配置已生成，但触发 SQL 尚未生成/)
    expect((screen.getByLabelText('任务名称 *') as HTMLInputElement).value).toBe('AI 测试配置')
    expect((screen.getByLabelText('触发 SQL 条件（待审核）') as HTMLTextAreaElement).value).toBe('')
    sqlGenerateResponse = { status: 200, body: { sql: 'SELECT true AS should_trigger', review: { window: 'week' }, warnings: [] } }
    await user.click(screen.getByRole('button', { name: '用 Genie 生成 SQL（会查询数据）' }))
    await waitFor(() => expect((screen.getByLabelText('触发 SQL 条件（待审核）') as HTMLTextAreaElement).value).toBe('SELECT true AS should_trigger'))
    expect(calls.filter(c => c.url === '/api/tasks/generate')).toHaveLength(1)
    expect(calls.filter(c => c.url === '/api/conditions/sql/generate')).toHaveLength(2)
    expect(calls.some(c => /\/test$|\/create$/.test(c.url))).toBe(false)
  })
  it('generates once, allows edits, previews, and saves only after explicit confirmation', async () => {
    const user = await openForm()
    await user.type(screen.getByLabelText('用自然语言描述任务'), '如果下降15%，每周一分析GMV')
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    await waitFor(() => expect((screen.getByLabelText('任务名称 *') as HTMLInputElement).value).toBe('AI 测试配置'))
    expect(screen.getByText('确认GMV口径')).toBeTruthy()
    expect(calls.filter(c => c.url === '/api/tasks/generate')).toHaveLength(1)
    expect(calls.filter(c => c.url === '/api/tasks/create')).toHaveLength(0)
    await user.clear(screen.getByLabelText('任务名称 *'))
    await user.type(screen.getByLabelText('任务名称 *'), '用户修改的周报')
    await user.selectOptions(screen.getByLabelText('星期'), 'friday')
    await user.selectOptions(screen.getByLabelText('检查时间'), '10:00')
    await user.click(screen.getByRole('button', { name: '预览并保存' }))
    expect(screen.getByText('每周五 10:00 · Asia/Shanghai')).toBeTruthy()
    expect(calls.filter(c => c.url === '/api/tasks/create')).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const saved = calls.find(c => c.url === '/api/tasks/create')!.body
    expect(saved.name).toBe('用户修改的周报')
    expect(saved.enabled).toBe(false)
    expect((saved.trigger as { type: string }).type).toBe('agent')
    expect((saved.schedule as { weekday: string }).weekday).toBe('friday')
    expect((saved.report as { draft_review: unknown }).draft_review).toBeTruthy()
    expect(screen.getByRole('log', { name: '对话记录' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '审核启用' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '查看此任务的分析报告' })).toBeTruthy()
  })

  it('saves explicit recipients, template and curated CSV choice without sending mail', async () => {
    const user = await openForm()
    const previousFetch = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (url === '/api/report-options') return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({
        templates: [{ id: 'default', label: '标准业务报告' }], csv_exports: [{ id: 'wide', label: '业务宽表' }], email_enabled: false,
      }) } as Response)
      return previousFetch(url, init)
    }))
    await user.type(screen.getByLabelText('任务名称 *'), '邮件日报')
    await user.click(screen.getByLabelText('生成报告后发送邮件'))
    await screen.findByLabelText('业务宽表')
    await user.type(screen.getByLabelText('收件人邮箱 *'), 'recipient@example.com')
    await user.click(screen.getByLabelText('业务宽表'))
    await user.click(screen.getByRole('button', { name: '预览并保存' }))
    expect(screen.getByText(/收件人：recipient@example.com/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await waitFor(() => expect(calls.some(c => c.url === '/api/tasks/create')).toBe(true))
    const report = calls.find(c => c.url === '/api/tasks/create')!.body.report as { delivery: unknown }
    expect(report.delivery).toEqual({ enabled: true, to: ['recipient@example.com'], cc: [], template_id: 'default', subject_template: '', csv_exports: ['wide'] })
    expect(calls.some(c => /send|gmail/.test(c.url))).toBe(false)
  })

  it('creates and confirms a task inside the conversation with prior analysis and no implicit parser call', async () => {
    render(<App/>)
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox', { name: '输入业务问题' }), '分析善存的GMV')
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByText('fixture：讨论善存的GMV与周度变化。')
    await user.type(screen.getByRole('textbox', { name: '输入业务问题' }), '和前一周相比呢？')
    await user.click(screen.getByTitle('发送消息'))
    await waitFor(() => expect(calls.filter(call => call.url === '/api/chat')).toHaveLength(2))
    await waitFor(() => expect(screen.getAllByText('fixture：讨论善存的GMV与周度变化。')).toHaveLength(2))
    expect(calls.filter(call => call.url === '/api/chat')[1].body.conversation_id).toBe('fixture-c')
    const text = '把这个设成每周任务'
    await user.type(screen.getByRole('textbox', { name: '输入业务问题' }), text)
    await user.click(screen.getByTitle('发送消息'))
    await screen.findByRole('region', { name: '对话任务草稿' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect((screen.getByLabelText('对话中的任务要求') as HTMLTextAreaElement).value).toBe(text)
    expect(calls.filter(call => call.url === '/api/chat')).toHaveLength(2)
    expect(calls.some(call => call.url === '/api/tasks/generate')).toBe(false)
    expect(screen.getByText('系统 · 任务配置引导')).toBeTruthy()
    const config = buildCreatePayload({ ...EMPTY_FORM, task_id: 'conversation-fixture', name: '善存周度分析', schedule_freq: 'weekly', report_prompt: '分析善存GMV',
      draft_review: { original_request: text, conditional: false, assumptions: [], warnings: [], missing_information: [], source_agent: 'genie_a1',
        conversation: [{ role: 'user', content: '分析善存的GMV' }] } })
    generateResponse = { status: 200, body: { configuration: config } }
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    await screen.findByRole('heading', { name: '善存周度分析' })
    const generated = calls.find(call => call.url === '/api/tasks/generate')!.body
    expect(generated.requirements).toBe(text)
    expect(generated.source_agent).toBe('genie_a1')
    expect((generated.conversation as unknown[])).toHaveLength(4)
    expect(calls.some(call => call.url === '/api/tasks/create')).toBe(false)
    await user.click(screen.getByLabelText('我已核对配置；保存为草稿，不会启用或发送报告'))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await screen.findByText(/已保存任务草稿「善存周度分析」/)
    expect(screen.getByRole('heading', { name: '与数据对话' })).toBeTruthy()
    expect(calls.filter(call => call.url === '/api/tasks/create')).toHaveLength(1)
    expect(calls.find(call => call.url === '/api/tasks/create')!.body.enabled).toBe(false)
    await user.click(screen.getByRole('button', { name: '查看我的任务' }))
    expect(screen.getByRole('heading', { name: '我的任务' })).toBeTruthy()
    await screen.findByRole('heading', { name: '善存周度分析' })
  })

  it('shows missing model error and still permits manual creation', async () => {
    generateResponse = { status: 503, body: { detail: 'Model not configured：仍可手动创建草稿。' } }
    const user = await openForm()
    await user.type(screen.getByLabelText('用自然语言描述任务'), '每日报告')
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    await screen.findByText('Model not configured：仍可手动创建草稿。')
    await user.type(screen.getByLabelText('任务名称 *'), '手动日报')
    await user.click(screen.getByRole('button', { name: '预览并保存' }))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await waitFor(() => expect(calls.filter(c => c.url === '/api/tasks/create')).toHaveLength(1))
  })

  it('edits a conversation proposal in the shared form and saves back to the same card', async () => {
    render(<App/>)
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox', { name: '输入业务问题' }), '每周五生成善存GMV报告')
    await user.click(screen.getByRole('button', { name: '设置自动任务' }))
    expect(calls.some(call => call.url === '/api/tasks/generate' || call.url === '/api/chat')).toBe(false)
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    await screen.findByRole('button', { name: '审核并编辑' })
    await user.click(screen.getByRole('button', { name: '审核并编辑' }))
    await screen.findByRole('dialog')
    await user.clear(screen.getByLabelText('任务名称 *'))
    await user.type(screen.getByLabelText('任务名称 *'), '修改后的对话任务')
    await user.click(screen.getByRole('button', { name: '预览并保存' }))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await screen.findByText(/已保存任务草稿「修改后的对话任务」/)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('heading', { name: '与数据对话' })).toBeTruthy()
    expect(calls.filter(call => call.url === '/api/tasks/generate')).toHaveLength(1)
    expect(calls.filter(call => call.url === '/api/tasks/create')).toHaveLength(1)
    expect(calls.find(call => call.url === '/api/tasks/create')!.body.name).toBe('修改后的对话任务')
  })

  it('preserves edited configuration after save error and never auto retries', async () => {
    saveStatus = 502
    const user = await openForm()
    await user.type(screen.getByLabelText('任务名称 *'), '手动保留配置')
    await user.click(screen.getByRole('button', { name: '预览并保存' }))
    await user.click(screen.getByRole('button', { name: '确认保存为草稿' }))
    await screen.findByText('保存失败，测试错误')
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText('手动保留配置')).toBeTruthy()
    expect(calls.filter(c => c.url === '/api/tasks/create')).toHaveLength(1)
  })

  it('shows drafts separately in dashboard counts', async () => {
    await openForm()
    const enabledCard = screen.getByText('已启用', { selector: '.metric-card>span' }).closest('.metric-card')!
    const draftCard = screen.getByText('草稿', { selector: '.metric-card>span' }).closest('.metric-card')!
    expect(within(enabledCard as HTMLElement).getByText('1')).toBeTruthy()
    expect(within(draftCard as HTMLElement).getByText('1')).toBeTruthy()
  })

  it('ignores a late generation response after closing and reopening the form', async () => {
    const user = await openForm()
    let resolve!: (value: unknown) => void
    const previousFetch = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (url === '/api/tasks/generate') return new Promise(r => { resolve = r })
      return previousFetch(url, init)
    }))
    await user.type(screen.getByLabelText('用自然语言描述任务'), '每周报告')
    await user.click(screen.getByRole('button', { name: '生成任务配置' }))
    expect(screen.getByRole('button', { name: '正在生成…' }).hasAttribute('disabled')).toBe(true)
    await user.click(screen.getByRole('button', { name: '关闭创建任务' }))
    await user.click(screen.getByRole('button', { name: '创建任务' }))
    resolve({ ok: true, status: 200, text: async () => JSON.stringify({ configuration: generatedFixture() }) })
    await waitFor(() => expect((screen.getByLabelText('任务名称 *') as HTMLInputElement).value).toBe(''))
    expect(screen.queryByText('确认GMV口径')).toBeNull()
  })
})
