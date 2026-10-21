// @vitest-environment jsdom
import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConditionSQLFields } from './ConditionSQLFields'
import { EMPTY_FORM, type TaskFormState } from './taskDrafts'

afterEach(cleanup)

it('requires separate explicit generation and testing and keeps SQL review in form state', async () => {
  const request = vi.fn().mockResolvedValueOnce({ sql: 'SELECT false AS should_trigger', review: { window: 'week' }, warnings: [] })
    .mockResolvedValueOnce({ window: 'week', bindings: { business_date: '2025-06-20' }, test: { should_trigger: false, reason: 'fixture condition not met', receipt: 'fixture-receipt' } })
  function Harness() {
    const [form,setForm] = useState<TaskFormState>({ ...EMPTY_FORM, trigger_type: 'agent', trigger_prompt: 'GMV下降超过15%' })
    return <><ConditionSQLFields form={form} setForm={setForm} request={request}/><pre data-testid="state">{JSON.stringify(form)}</pre></>
  }
  const user = userEvent.setup()
  render(<Harness/>)
  expect(request).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: '用 Genie 生成 SQL（会查询数据）' }))
  await waitFor(() => expect(JSON.parse(screen.getByTestId('state').textContent!).trigger_type).toBe('sql'))
  expect(request).toHaveBeenCalledTimes(1)
  await user.click(screen.getByRole('button', { name: '运行一次只读条件测试' }))
  await screen.findByText(/只读条件测试成功：条件不成立/)
  expect(JSON.parse(screen.getByTestId('state').textContent!).sql_review.test.receipt).toBe('fixture-receipt')
  expect(request).toHaveBeenCalledTimes(2)
  expect(request.mock.calls[1][0]).toBe('/api/conditions/sql/test')
  expect(JSON.parse(request.mock.calls[1][1].body).confirm).toBe(true)
})

it('AI test only runs on click, saves its own review and invalidates it when Agent changes', async () => {
  const request = vi.fn().mockResolvedValue({ window: 'week', test: { should_trigger: false, reason: 'fixture: no strategy change', receipt: 'fixture-ai-receipt' } })
  function Harness() {
    const [form,setForm] = useState<TaskFormState>({ ...EMPTY_FORM, schedule_freq: 'weekly', trigger_type: 'agent', trigger_agent: 'genie_a2', condition_agents: ['genie_a2'], trigger_prompt: '竞品策略是否有实质变化' })
    return <><ConditionSQLFields form={form} setForm={setForm} request={request}/><pre data-testid="state">{JSON.stringify(form)}</pre></>
  }
  const user = userEvent.setup()
  render(<Harness/>)
  expect(request).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: '运行一次 AI 条件测试（会查询数据）' }))
  await screen.findByText(/AI 条件测试成功：条件不成立/)
  expect(request).toHaveBeenCalledTimes(1)
  expect(request.mock.calls[0][0]).toBe('/api/conditions/agent/test')
  expect(JSON.parse(request.mock.calls[0][1].body)).toMatchObject({ condition: '竞品策略是否有实质变化', agent: 'genie_a2', confirm: true })
  expect(JSON.parse(screen.getByTestId('state').textContent!).agent_review.test.receipt).toBe('fixture-ai-receipt')
  await user.selectOptions(screen.getByLabelText('条件参考 Agent'), 'genie_a1')
  expect(JSON.parse(screen.getByTestId('state').textContent!).agent_review).toEqual({})
})
