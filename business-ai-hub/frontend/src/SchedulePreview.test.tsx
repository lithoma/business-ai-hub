// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SchedulePreview } from './SchedulePreview'
afterEach(cleanup)
it('previews a non-09:00 plan without a mutation or execution call', async () => {
  const request=vi.fn(async()=>({scheduler_enabled:false,tasks:[{task_id:'fixture',name:'fixture task',active:true,due:false,next_run_at:'2026-10-23T14:00:00+08:00'}]}))
  render(<SchedulePreview request={request as never}/>)
  await userEvent.click(screen.getByRole('button',{name:'调度计划'}))
  expect(await screen.findByText(/2026-10-23 14:00/)).toBeTruthy()
  expect(screen.getByText(/自动运行未开启/)).toBeTruthy()
  expect(request).toHaveBeenCalledExactlyOnceWith('/api/scheduler/plan')
})
