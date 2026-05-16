import { getToken } from './auth'

const BASE = import.meta.env.VITE_API_URL || ''

function headers(): HeadersInit {
  const token = getToken()
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: headers() })
  if (res.status === 401) { window.dispatchEvent(new Event('auth:logout')); throw new Error('Não autenticado') }
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', headers: headers(), body: JSON.stringify(body) })
  if (res.status === 401) { window.dispatchEvent(new Event('auth:logout')); throw new Error('Não autenticado') }
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'PATCH', headers: headers(), body: JSON.stringify(body) })
  if (res.status === 401) { window.dispatchEvent(new Event('auth:logout')); throw new Error('Não autenticado') }
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

async function del<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'DELETE', headers: headers(), body: body ? JSON.stringify(body) : undefined })
  if (res.status === 401) { window.dispatchEvent(new Event('auth:logout')); throw new Error('Não autenticado') }
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function* streamAgent(path: string, body: unknown): AsyncGenerator<{ type: string; payload: string }> {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', headers: headers(), body: JSON.stringify(body) })
  if (res.status === 401) { window.dispatchEvent(new Event('auth:logout')); throw new Error('Não autenticado') }
  if (!res.ok) throw new Error(await res.text())

  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      yield JSON.parse(line.slice(6))
    }
  }
}

export const api = {
  auth: {
    login: (email: string, password: string) => post('/auth/login', { email, password }),
    me: () => get('/auth/me'),
    users: () => get('/auth/users'),
    createUser: (data: unknown) => post('/auth/users', data),
    grantAccess: (userId: number, clientId: number) => post('/auth/grant-access', { user_id: userId, client_id: clientId }),
    revokeAccess: (userId: number, clientId: number) => del('/auth/revoke-access', { user_id: userId, client_id: clientId }),
    listAccess: (clientId: number) => get(`/auth/access/${clientId}`),
  },
  clients: {
    list: () => get('/clients/'),
    create: (data: unknown) => post('/clients/', data),
    get: (id: number) => get(`/clients/${id}`),
    update: (id: number, data: unknown) => patch(`/clients/${id}`, data),
    refreshScore: (id: number) => post(`/clients/${id}/refresh-score`, {}),
    scoreHistory: (id: number, days?: number) => get(`/clients/${id}/score-history${days ? `?days=${days}` : ''}`),
  },
  content: {
    list: (clientId: number, status?: string) => get(`/content/client/${clientId}${status ? `?status=${status}` : ''}`),
    create: (data: unknown) => post('/content/', data),
    get: (id: number) => get(`/content/${id}`),
    update: (id: number, data: unknown) => patch(`/content/${id}`, data),
    approve: (id: number) => post(`/content/${id}/approve`, {}),
    regenerateBrief: (id: number) => post(`/content/${id}/regenerate-brief`, {}),
    regenerateSection: (id: number, section: string, instruction?: string) =>
      post(`/content/${id}/regenerate-section`, { section, instruction: instruction || null }),
    hookVariations: (id: number, count?: number, instruction?: string) =>
      post(`/content/${id}/hook-variations`, { count: count || 3, instruction: instruction || null }),
    selectHook: (id: number, hook: string) => post(`/content/${id}/select-hook`, { hook }),
    bulkApprove: (ids: number[]) => post('/content/bulk/approve', { ids }),
    bulkDelete: (ids: number[]) => post('/content/bulk/delete', { ids }),
  },
  calendar: {
    get: (clientId: number, days?: number) => get(`/calendar/client/${clientId}${days ? `?days=${days}` : ''}`),
    generateWeek: (data: unknown) => post('/calendar/generate-week', data),
    attachContent: (slotId: number, contentId: number) => patch(`/calendar/${slotId}/attach`, { content_id: contentId }),
    populateFromWeekly: (data: { client_id: number; start_date?: string; platform?: string; default_hour?: number }) => post('/calendar/populate-from-weekly', data),
  },
  analytics: {
    summary: (clientId: number, days?: number) => get(`/analytics/client/${clientId}/summary${days ? `?days=${days}` : ''}`),
    metrics: (clientId: number) => get(`/analytics/client/${clientId}/metrics`),
    addMetrics: (data: unknown) => post('/analytics/metrics', data),
  },
  social: {
    list: (clientId: number) => get(`/social/client/${clientId}`),
    upsert: (data: { client_id: number; platform: string; account_id: string; account_name?: string; access_token: string }) => post('/social/', data),
    update: (id: number, data: unknown) => patch(`/social/${id}`, data),
    remove: (id: number) => del(`/social/${id}`),
    test: (id: number) => post(`/social/${id}/test`, {}),
    publish: (contentId: number) => post(`/social/publish/${contentId}`, {}),
  },
  agents: {
    strategy: (clientId: number, period?: string) => streamAgent('/agents/strategy/stream', { client_id: clientId, period }),
    analytics: (clientId: number, metricsData: string) => streamAgent('/agents/analytics/stream', { client_id: clientId, metrics_data: metricsData }),
    script: (clientId: number, topic: string, format: string, platform: string, objective: string) => streamAgent('/agents/script/stream', { client_id: clientId, topic, format, platform, objective }),
    trend: (clientId: number, currentTrends: string) => streamAgent('/agents/trend/stream', { client_id: clientId, current_trends: currentTrends }),
    design: (clientId: number, topic: string, format: string, platform: string, references?: string) => streamAgent('/agents/design/stream', { client_id: clientId, content_topic: topic, format, platform, references }),
    amplifier: (clientId: number, rawIdea: string) => streamAgent('/agents/amplifier/stream', { client_id: clientId, raw_idea: rawIdea }),
    auto: (clientId: number, siteUrl: string, topic: string, format: string, platform: string, objective?: string) =>
      streamAgent('/agents/auto/stream', { client_id: clientId, site_url: siteUrl, topic, format, platform, objective: objective || '' }),
  },
  persona: {
    get: (clientId: number) => get(`/persona/client/${clientId}`),
    generate: (clientId: number) => post(`/persona/client/${clientId}/generate`, {}),
  },
  inspirations: {
    list: (clientId: number) => get(`/inspirations/client/${clientId}`),
    create: (data: { client_id: number; source_type: string; source_value: string; label?: string }) => post('/inspirations/', data),
    remove: (id: number) => del(`/inspirations/${id}`),
  },
  products: {
    list: (clientId: number) => get(`/products/client/${clientId}`),
    create: (data: unknown) => post('/products/', data),
    update: (id: number, data: unknown) => patch(`/products/${id}`, data),
    remove: (id: number) => del(`/products/${id}`),
  },
  knowledge: {
    list: (clientId: number) => get(`/knowledge/client/${clientId}`),
    create: (data: { client_id: number; title: string; content: string; source_type?: string; tags?: string[] }) => post('/knowledge/', data),
    remove: (id: number) => del(`/knowledge/${id}`),
  },
  strategy: {
    weekly: (clientId: number) => get(`/strategy/weekly/${clientId}`),
    regenerateWeekly: (clientId: number) => post(`/strategy/weekly/${clientId}/generate`, {}),
    insights: (clientId: number) => get(`/strategy/insights/${clientId}`),
    regenerateInsights: (clientId: number) => post(`/strategy/insights/${clientId}/generate`, {}),
    dismissInsight: (insightId: number) => post(`/strategy/insights/${insightId}/dismiss`, {}),
    salesSequence: (clientId: number, data: { product_id: number; launch_date: string; total_days: number; platform?: string; generate_images?: boolean }) =>
      post(`/strategy/sales-sequence/${clientId}`, data),
    profileAudit: (clientId: number) => post(`/strategy/profile-audit/${clientId}`, {}),
  },
}
