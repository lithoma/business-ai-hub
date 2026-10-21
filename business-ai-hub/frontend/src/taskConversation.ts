import { isRecurringTaskRequest, type ConversationTurn, type DraftConfiguration } from './taskDrafts'

export type TaskSeed = { requirements: string; conversation: ConversationTurn[]; agent: 'genie_one'; configuration?: DraftConfiguration; saved?: boolean; traceId?: string; sqlState?: 'running' | 'done' | 'error'; sqlError?: string }

export function isTaskCreationIntent(text: string): boolean {
  if (/^(?:请)?(?:不要|不用|别)|\b(?:do not|don't)\s+(?:create|make|schedule)|怎么|如何|\bhow (?:do|can|to)\b/i.test(text)) return false
  return isRecurringTaskRequest(text) || (
    /创建|新建|设置|安排|设成|设为|变成|改成|\b(?:create|make|turn|schedule)\b/i.test(text)
    && /任务|监控|定期|每天|每日|每周|每星期|\b(?:task|monitor|daily|weekly|every)\b/i.test(text)
  )
}

export function conversationContext(messages: { role: 'user' | 'assistant'; text: string; source?: string; task?: TaskSeed }[]): ConversationTurn[] {
  const context: ConversationTurn[] = []
  let remaining = 24000
  for (const message of messages.slice().reverse()) {
    if (context.length === 12 || remaining === 0) break
    if (message.source || message.task) continue
    const content = message.text.trim().slice(0,Math.min(4000,remaining))
    if (!content) continue
    context.unshift({ role: message.role, content })
    remaining -= content.length
  }
  return context
}
