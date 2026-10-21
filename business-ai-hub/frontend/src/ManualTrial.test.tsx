// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TaskTrial } from './ManualTrial'

afterEach(cleanup)
it('requires trial confirmation, shows real phases/results, and sends only after separate email review (offline)', async () => {
  const calls: string[] = []
  const request = vi.fn(async (url: string) => {
    calls.push(url)
    return url.endsWith('/trial') ? { run_id:'fixture',reused:false }
      : url.endsWith('/email-preview') ? { sender:'sender@example.com',recipient:'recipient@example.com',subject:'fixture',body:'fixture report',preview_hash:'fixture-hash',can_send:true,notification_status:'NOT_QUEUED' }
      : url.endsWith('/send') ? { notification_status:'EMAIL_ACCEPTED' }
      : { run_id:'fixture',phase:'completed',status:'REPORT_CREATED',report_text:'fixture report',trigger_reason:'fixture condition',timings:{condition_seconds:1,report_seconds:2} }
  })
  render(<TaskTrial task={{ task_id:'fixture',name:'fixture task',updated_at:'v1' }} settings={{ enabled:true }} request={request as never} onViewReports={vi.fn()}/>)
  const user=userEvent.setup()
  await user.click(screen.getByRole('button',{name:'试运行一次'}))
  await user.type(screen.getByLabelText('试运行业务日期'),'2025-06-24')
  const start=screen.getByRole('button',{name:'确认试运行一次'})
  expect((start as HTMLButtonElement).disabled).toBe(true)
  await user.click(screen.getByRole('checkbox'))
  await user.click(start)
  await screen.findByText('fixture report')
  expect(calls.filter(url=>url.endsWith('/trial'))).toHaveLength(1)
  expect(calls.some(url=>url.endsWith('/send'))).toBe(false)
  await user.click(screen.getByRole('button',{name:'预览邮件'}))
  await screen.findByText(/recipient@example.com/)
  expect((screen.getByRole('button',{name:'确认发送一次'}) as HTMLButtonElement).disabled).toBe(true)
  await user.click(screen.getByLabelText('我已核对收件人与报告，确认发送这一封邮件'))
  await user.click(screen.getByRole('button',{name:'确认发送一次'}))
  await waitFor(()=>expect(calls.filter(url=>url.endsWith('/send'))).toHaveLength(1))
  expect(await screen.findByText(/邮件服务已接收/)).toBeTruthy()
})
