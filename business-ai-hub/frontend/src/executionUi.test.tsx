// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Reports, TaskLifecycle } from './executionUi'

afterEach(cleanup)
const TASK = { task_id: 'fixture', name: '测试任务', lifecycle_status: 'draft' }
const READY = { errors: [], warnings: ['请核对生成假设'], execution_enabled: true, can_activate: true, updated_at: 'version1' }

describe('task lifecycle', () => {
  it('never activates until configuration was fetched and user reviewed and confirmed', async () => {
    const request = vi.fn().mockResolvedValueOnce(READY).mockResolvedValueOnce({ lifecycle_status: 'active' })
    const changed = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(<TaskLifecycle task={TASK} request={request} onChanged={changed} onEdit={vi.fn()}/>)
    await user.click(screen.getByRole('button', { name: '审核启用' }))
    const confirm = await screen.findByRole('button', { name: '确认审核启用' })
    expect(confirm.hasAttribute('disabled')).toBe(true)
    expect(request).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('checkbox'))
    await user.click(confirm)
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ action: 'activate', confirm: true, reviewed: true, expected_updated_at: 'version1' })
  })
  it('keeps activation disabled when rollout gate is closed or config is invalid', async () => {
    const user = userEvent.setup()
    const request = vi.fn().mockResolvedValue({ ...READY, errors: ['时间与现有 Job 不兼容'], execution_enabled: false, can_activate: false })
    render(<TaskLifecycle task={TASK} request={request} onChanged={vi.fn()} onEdit={vi.fn()}/>)
    await user.click(screen.getByRole('button', { name: '审核启用' }))
    await screen.findByText('时间与现有 Job 不兼容')
    await user.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '确认审核启用' }).hasAttribute('disabled')).toBe(true)
    expect(request).toHaveBeenCalledTimes(1)
  })
  it('shows a stale update error without retrying or closing review', async () => {
    const request = vi.fn().mockResolvedValueOnce(READY).mockRejectedValueOnce(new Error('任务已被修改，请刷新'))
    const user = userEvent.setup()
    render(<TaskLifecycle task={{ ...TASK, lifecycle_status: 'active' }} request={request} onChanged={vi.fn()} onEdit={vi.fn()}/>)
    await user.click(screen.getByRole('button', { name: '暂停任务' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '确认暂停任务' }).hasAttribute('disabled')).toBe(false))
    await user.click(screen.getByRole('button', { name: '确认暂停任务' }))
    await screen.findByText('任务已被修改，请刷新')
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(request).toHaveBeenCalledTimes(2)
  })
})

describe('report history', () => {
  it('displays persisted report, failure and queue states and sends explicit filters', async () => {
    const request = vi.fn().mockResolvedValue({ runs: [
      { run_id: '1', task_name: '销售风险', task_id: 'fixture', status: 'REPORT_CREATED', notification_status: 'QUEUED', report_text: '# 实际存储的测试报告', scheduled_slot: 'Monday', trigger_reason: '测试条件成立' },
      { run_id: '2', task_name: '失败任务', status: 'FAILED', notification_status: 'NOT_QUEUED', error_code: 'ValueError' },
    ], next_offset: null })
    const user = userEvent.setup()
    render(<Reports request={request}/>)
    await screen.findByText('销售风险')
    expect(screen.getByText('通知：已入队（未确认送达）')).toBeTruthy()
    await user.click(screen.getByText('查看报告'))
    expect(screen.getByRole('heading', { name: '实际存储的测试报告' })).toBeTruthy()
    expect(screen.getByText('失败类型：ValueError；请检查 Job 日志和 Agent 配置。')).toBeTruthy()
    await user.selectOptions(screen.getByLabelText('执行状态'), 'FAILED')
    await user.type(screen.getByLabelText('任务 ID'), 'fixture')
    await user.click(screen.getByRole('button', { name: '查询 / 刷新' }))
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    expect(request.mock.calls[1][0]).toContain('status=FAILED')
    expect(request.mock.calls[1][0]).toContain('task_id=fixture')
  })
  it('shows history unavailable errors and does not invent reports', async () => {
    const request = vi.fn().mockRejectedValue(new Error('报告历史暂不可用'))
    render(<Reports request={request}/>)
    await screen.findByRole('alert')
    expect(screen.getByText('报告历史暂不可用')).toBeTruthy()
    expect(screen.queryByText('查看报告')).toBeNull()
    expect(screen.queryByText('暂无执行记录。保存草稿不会生成报告。')).toBeNull()
    expect(request).toHaveBeenCalledTimes(1)
  })
})
