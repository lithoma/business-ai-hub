import { describe, expect, it } from 'vitest'
import { EMPTY_FORM, SCHEDULE_HOURS, buildCreatePayload, formError, proposalToForm, scheduleDescription, taskCounts } from './taskDrafts'

describe('draft statistics', () => {
  it('counts drafts separately even when legacy enabled flag is true', () => {
    expect(taskCounts([
      { _source: 'yaml', enabled: true }, { _source: 'yaml', enabled: 'false' },
      { _source: 'registry', lifecycle_status: 'draft', enabled: true },
      { _source: 'registry', lifecycle_status: 'draft', enabled: 'false' },
      { _source: 'registry', enabled: true }, { _source: 'yaml', enabled: 'unknown' },
    ])).toEqual({ total: 6, enabled: 1, disabled: 1, drafts: 3 })
  })
})

describe('schedule editing', () => {
  it('round trips weekly schedule, timezone and review without losing condition', () => {
    const form = { ...EMPTY_FORM, task_id: 'a', name: 'weekly', schedule_freq: 'weekly' as const,
      schedule_weekday: 'sunday', schedule_time: '18:00', schedule_timezone: 'UTC',
      trigger_type: 'agent' as const, trigger_prompt: 'GMV下降超过15%',
      draft_review: { original_request: '如果下降15%', conditional: true, assumptions: [], warnings: [], missing_information: [] },
    }
    const payload = buildCreatePayload(form)
    expect(payload.schedule.cron).toBe('0 18 * * 0')
    expect(payload.enabled).toBe(false)
    expect(proposalToForm(payload)).toEqual(form)
    expect(scheduleDescription(payload.schedule)).toBe('每周日 18:00 · UTC')
    expect(SCHEDULE_HOURS).toHaveLength(24)
    expect(SCHEDULE_HOURS.every(time => time.endsWith(':00'))).toBe(true)
  })

  it('uses a start date for intervals instead of misleading monthly cron', () => {
    const form = { ...EMPTY_FORM, schedule_freq: 'interval' as const, interval_days: 3, schedule_start_date: '2026-10-20' }
    const payload = buildCreatePayload(form)
    expect(payload.schedule.cron).toBe('')
    expect(payload.schedule.start_date).toBe('2026-10-20')
    expect(scheduleDescription(payload.schedule)).toContain('每 3 天，从 2026-10-20 开始')
  })

  it('rejects invalid time, timezone, fractional interval and conditional downgrade', () => {
    const valid = { ...EMPTY_FORM, task_id: 'a', name: 'name' }
    expect(formError(valid)).toBeNull()
    for (const changes of [
      { schedule_time: '25:00' }, { schedule_time: '09:30' }, { schedule_timezone: 'Bogus' },
      { schedule_freq: 'interval' as const, interval_days: 1.5 },
      { draft_review: { original_request: '如果下降', conditional: true, assumptions: [], warnings: [], missing_information: [] } },
    ]) expect(formError({ ...valid, ...changes })).not.toBeNull()
  })
})
