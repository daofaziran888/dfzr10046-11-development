import { ref } from 'vue'

/** 审计派办全模块共享的“当前操作人”身份；URL ?as= 优先，其次本地记忆。 */
const stored = typeof localStorage !== 'undefined' ? localStorage.getItem('audit_operator') : ''
export const operator = ref(stored || '主管')

export const OPERATOR_CHOICES = ['主管', '甲', '乙', '丙']

export function setOperator(value: string) {
  const v = value.trim()
  operator.value = v
  try {
    localStorage.setItem('audit_operator', v)
  } catch {
    /* 隐私模式下忽略 */
  }
}

const PREFERRED: Record<string, string> = {
  handle: '甲',
  review: '丙',
}

/** 路由页初始化身份：?as/?operator/?user/?assignee/?owner 均可，其次本地记忆。 */
export function pickQueryIdentity(query: Record<string, unknown>): string {
  for (const key of ['as', 'operator', 'user', 'assignee', 'owner', 'operator_id', 'username']) {
    const v = query[key]
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return ''
}

/** 路由页初始化身份：URL 身份参数优先；接单页缺省给场景默认角色。 */
export function initOperator(queryAs: unknown, page?: 'handle' | 'review'): string {
  if (typeof queryAs === 'string' && queryAs.trim()) {
    setOperator(queryAs.trim())
    return operator.value
  }
  if (!operator.value || operator.value === '主管') {
    const fallback = page ? PREFERRED[page] : ''
    if (fallback) setOperator(fallback)
  }
  return operator.value
}
