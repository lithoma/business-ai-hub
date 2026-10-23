"""Calendar-only planning. No timers, Spark, SDK, model calls or resource changes."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

DAYS = ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')


def validate(schedule, defaults=None):
    value = dict(defaults or {}, **schedule)
    if value.get('frequency') not in ('daily','weekly','interval'):
        raise ValueError('不支持的调度频率。')
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', value.get('time','')):
        raise ValueError('检查时间必须为 HH:MM。')
    try:ZoneInfo(value['timezone'])
    except (KeyError,TypeError,ZoneInfoNotFoundError) as exc:
        raise ValueError('请选择有效的 IANA 时区。') from exc
    if value['frequency']=='weekly' and value.get('weekday','monday') not in DAYS:
        raise ValueError('星期无效。')
    anchor = date.fromisoformat(value['start_date']) if value.get('start_date') else None
    if value['frequency']=='interval' and (not anchor or type(value.get('interval_days')) is not int or not 1<=value['interval_days']<=365):
        raise ValueError('间隔任务需要 1–365 天及开始日期。')
    return value, anchor


def eligible(day, schedule, anchor):
    if anchor and day<anchor:return False
    if schedule['frequency']=='weekly':return DAYS[day.weekday()]==schedule.get('weekday','monday')
    if schedule['frequency']=='interval':return (day-anchor).days % schedule['interval_days']==0
    return True


def instant(day, schedule):
    """First occurrence on DST fallback; first valid minute after a spring gap."""
    naive=datetime.combine(day,time.fromisoformat(schedule['time']))
    zone=ZoneInfo(schedule['timezone'])
    for minutes in range(181):
        candidate=(naive+timedelta(minutes=minutes)).replace(tzinfo=zone,fold=0)
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)==candidate.replace(tzinfo=None):
            return candidate
    raise ValueError('该日期的本地检查时间不存在。')


def plan(schedule, now, defaults=None):
    if now.tzinfo is None:raise ValueError('调度时间必须包含时区。')
    value,anchor=validate(schedule,defaults)
    local=now.astimezone(ZoneInfo(value['timezone']))
    today=local.date()
    due_at=instant(today,value) if eligible(today,value,anchor) else None
    due=due_at is not None and now.astimezone(timezone.utc)>=due_at.astimezone(timezone.utc)
    next_at=None
    # At most a year for supported intervals; a future start date may be farther away.
    first=max(today,anchor) if anchor else today
    for offset in range(367):
        day=first+timedelta(days=offset)
        if eligible(day,value,anchor):
            candidate=instant(day,value)
            if candidate.astimezone(timezone.utc)>now.astimezone(timezone.utc):
                next_at=candidate.isoformat();break
    slot=today.isoformat()+'T'+value['time']+'['+value['timezone']+']' if due else None
    return {'due':due,'slot':slot,'dispatch_date':today.isoformat(),
            'next_run_at':next_at,'timezone':value['timezone'],
            'policy':'同一日同一计划只运行一次；不自动补跑历史日期；夏令时重复时刻仅一次，缺失时刻顺延。'}
