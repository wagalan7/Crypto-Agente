export interface Client {
  id: number
  name: string
  niche: string | null
  target_audience: string | null
  tone: string | null
  personality: string | null
  positioning: string | null
  goals: string[]
  platforms: string[]
  authority_score: number
  created_at: string | null
}

export interface ContentPiece {
  id: number
  client_id: number
  title: string
  format: string
  platform: string
  objective: string
  hook: string | null
  script: string | null
  copy: string | null
  design_brief: string | null
  media_url: string | null
  status: 'pending' | 'approved' | 'recorded' | 'published'
  trend_context: string | null
  strategic_note: string | null
  scheduled_at: string | null
  published_at: string | null
  external_post_id: string | null
  publish_error: string | null
  created_at: string | null
}

export interface SocialAccount {
  id: number
  client_id: number
  platform: 'instagram' | 'facebook'
  account_id: string
  account_name: string | null
  access_token_preview: string
  is_active: boolean
  last_error: string | null
  expires_at: string | null
  updated_at: string | null
}

export interface CalendarSlot {
  id: number
  client_id: number
  content_id: number | null
  scheduled_at: string
  platform: string
  format: string
  objective: string
  status: 'planned' | 'ready' | 'published'
}

export interface MetricsSummary {
  client_id: number
  period_days: number
  content_count: number
  totals: {
    views: number
    likes: number
    comments: number
    shares: number
    saves: number
    reach: number
  }
  averages: {
    retention_rate: number
    ctr: number
    conversion_rate: number
  }
}

export interface MetricsSnapshot {
  id: number
  client_id: number
  content_id: number | null
  platform: string
  views: number
  likes: number
  comments: number
  shares: number
  saves: number
  reach: number
  retention_rate: number
  ctr: number
  conversion_rate: number
  recorded_at: string
}

export type AgentType = 'strategy' | 'analytics' | 'script' | 'trend' | 'design' | 'amplifier'

export interface SSEEvent {
  type: 'status' | 'chunk' | 'done'
  payload: string
}

export const OBJECTIVE_LABELS: Record<string, string> = {
  attract: 'Atrair',
  connect: 'Conectar',
  authority: 'Autoridade',
  sell: 'Vender',
  break_objection: 'Quebrar Objeção',
  retention: 'Retenção',
}

export const OBJECTIVE_COLORS: Record<string, string> = {
  attract: 'bg-blue-900/40 text-blue-300 border-blue-700',
  connect: 'bg-green-900/40 text-green-300 border-green-700',
  authority: 'bg-violet-900/40 text-violet-300 border-violet-700',
  sell: 'bg-orange-900/40 text-orange-300 border-orange-700',
  break_objection: 'bg-red-900/40 text-red-300 border-red-700',
  retention: 'bg-cyan-900/40 text-cyan-300 border-cyan-700',
}

export const FORMAT_LABELS: Record<string, string> = {
  reels: 'Reels',
  shorts: 'Shorts',
  story: 'Story',
  carousel: 'Carrossel',
  post: 'Post',
  youtube: 'YouTube',
}

export const STATUS_LABELS: Record<string, string> = {
  pending: 'Pendente',
  approved: 'Aprovado',
  recorded: 'Gravado',
  published: 'Publicado',
}

export const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-gray-700 text-gray-300',
  approved: 'bg-green-900/40 text-green-300',
  recorded: 'bg-blue-900/40 text-blue-300',
  published: 'bg-violet-900/40 text-violet-300',
}
