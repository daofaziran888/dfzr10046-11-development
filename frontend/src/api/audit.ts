/** 审计派办 API：所有调用都显式携带操作人身份与接单地址令牌。 */

export type AnyRow = Record<string, any>

const API_BASE = '/api/audit'

function identityHeaders(operator: string, token?: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  // HTTP 头只允许 ISO-8859-1；甲/乙/丙等中文身份走 query/body（已编码），
  // 这里仅在值为 Latin-1 时附带请求头作为额外通道。
  if (operator && /^[\u0000-ÿ]*$/.test(operator)) headers['X-Operator'] = operator
  if (token) headers['X-Assignment-Token'] = token
  return headers
}

function withIdentity(path: string, operator: string, token?: string): string {
  const params = new URLSearchParams()
  if (operator) params.set('as', operator)
  if (token) params.set('token', token)
  const qs = params.toString()
  if (!qs) return path
  return `${path}${path.includes('?') ? '&' : '?'}${qs}`
}

async function parse<T>(response: Response): Promise<T> {
  const text = await response.text()
  try {
    return JSON.parse(text) as T
  } catch {
    throw new Error(`接口返回非 JSON（HTTP ${response.status}）：${text.slice(0, 120)}`)
  }
}

export async function auditGet<T = AnyRow>(
  path: string, operator = '', token = '',
): Promise<T> {
  const response = await fetch(withIdentity(path, operator, token), {
    headers: identityHeaders(operator, token),
  })
  return parse<T>(response)
}

export async function auditPost<T = AnyRow>(
  path: string,
  body: Record<string, unknown> = {},
  operator = '',
  token = '',
): Promise<T> {
  const response = await fetch(withIdentity(path, operator, token), {
    method: 'POST',
    headers: identityHeaders(operator, token),
    body: JSON.stringify({ ...body, ...(operator ? { operator } : {}), ...(token ? { token } : {}) }),
  })
  return parse<T>(response)
}

export const auditApi = {
  feature: () => auditGet(`${API_BASE}/feature`),
  seedScenario: (reset = false) =>
    auditPost(`${API_BASE}/scenario`, reset ? { reset: '1' } : {}),
  listDispatches: (operator = '') => auditGet(`${API_BASE}/dispatch`, operator),
  detail: (id: number | string, operator = '', token = '') =>
    auditGet(`${API_BASE}/dispatch/${id}`, operator, token),
  create: (body: Record<string, unknown>, operator = '主管') =>
    auditPost(`${API_BASE}/dispatch`, body, operator),
  accept: (id: number | string, operator: string, token = '', kind?: string) =>
    auditPost(`${API_BASE}/dispatch/${id}/accept`, kind ? { kind } : {}, operator, token),
  saveOpinion: (id: number | string, content: string, operator: string, token = '') =>
    auditPost(`${API_BASE}/dispatch/${id}/opinion`, { content }, operator, token),
  submit: (id: number | string, operator: string, token = '', expectedVersion?: number) =>
    auditPost(`${API_BASE}/dispatch/${id}/submit`,
      expectedVersion ? { expected_version: expectedVersion } : {}, operator, token),
  reassign: (id: number | string, newHandler: string, operator = '主管',
             expectedVersion?: number) =>
    auditPost(`${API_BASE}/dispatch/${id}/reassign`,
      { new_handler: newHandler, ...(expectedVersion ? { expected_version: expectedVersion } : {}) },
      operator),
  inherit: (id: number | string, operator: string, token = '', andSubmit = false) =>
    auditPost(`${API_BASE}/dispatch/${id}/inherit`, { and_submit: andSubmit }, operator, token),
  material: (id: number | string, operator: string, token = '') =>
    auditGet(`${API_BASE}/dispatch/${id}/review-material`, operator, token),
  verdict: (id: number | string, verdict: string, comment: string,
            operator = '丙', token = '') =>
    auditPost(`${API_BASE}/dispatch/${id}/verdict`, { verdict, comment }, operator, token),
  todos: (owner: string) => auditGet(`${API_BASE}/todos?owner=${encodeURIComponent(owner)}`),
  opinions: (dispatchId: number | string) =>
    auditGet(`${API_BASE}/opinions?dispatch_id=${dispatchId}`),
  logs: (dispatchId: number | string, onlyRejected = false) =>
    auditGet(`${API_BASE}/request-logs?dispatch_id=${dispatchId}${onlyRejected ? '&only_rejected=1' : ''}`),
}
