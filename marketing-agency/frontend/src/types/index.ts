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
  objective_reasoning?: string | null
  emotion_used?: string | null
  funnel_stage?: string | null
  format_reasoning?: string | null
  linked_product_id?: number | null
  linked_product_name?: string | null
  production_brief?: ProductionBrief | null
  voice_score?: number | null
  voice_feedback?: { verdict: string; weakest_part: string | null; fix_hint: string } | null
  edit_count?: number | null
  review_notes?: string | null
}

export interface ContentVersion {
  id: number
  content_id: number
  version_number: number
  title: string | null
  hook: string | null
  script: string | null
  copy: string | null
  design_brief: string | null
  change_summary: string | null
  edited_by_user: boolean
  created_at: string | null
}

export interface ProductionBrief {
  location?: string
  wardrobe?: string
  props?: string[]
  shots?: Array<{ order: number; type: string; description: string }>
  audio?: string
  lighting?: string
  captions_overlay?: string[]
  duration_estimate_seconds?: number
  equipment_minimum?: string[]
  production_tips?: string[]
  edit_notes?: string
}

export interface Persona {
  id: number
  client_id: number
  pains: string[]
  desires: string[]
  emotions: string[]
  insecurities: string[]
  audience_goals: string[]
  language_patterns: string
  psychological_patterns: string
  audience_profile: string
  evidence: string
  generated_at: string | null
  user_refinements?: Array<{ field: string; note?: string; previous?: unknown; at?: string }>
  edit_count?: number
}

export interface Inspiration {
  id: number
  client_id: number
  source_type: 'url' | 'text' | 'image'
  source_value: string
  label: string | null
  analysis: Record<string, unknown>
  visual_analysis?: Record<string, unknown> | null
  image_url?: string | null
  adapted_brief: string | null
  created_at: string | null
}

export interface Insight {
  id: number
  client_id: number
  kind: string
  title: string
  message: string
  evidence: string | null
  severity: 'info' | 'warning' | 'critical' | 'opportunity'
  is_dismissed: boolean
  created_at: string | null
}

export interface Product {
  id: number
  client_id: number
  name: string
  type: string
  price: string | null
  description: string | null
  pains_solved: string[]
  desires: string[]
  objections: string[]
  transformation: string | null
  awareness_stage: string | null
  funnel_stage: string | null
  is_primary: boolean
  is_active: boolean
  created_at: string | null
}

export interface KnowledgeItem {
  id: number
  client_id: number
  title: string
  content: string
  source_type: string
  tags: string[]
  summary?: string | null
  key_insights?: string[] | null
  voice_signals?: string[] | null
  use_count?: number | null
  last_used_at?: string | null
  created_at: string | null
}

export interface WeeklyBrain {
  id: number
  client_id: number
  focus: string
  opportunities: string[]
  alerts: string[]
  risks: string[]
  priorities: string[]
  audience_behavior: string | null
  trends: string[]
  emotional_sequence: Array<{ day: string; emotion: string; intent: string; format_suggestion: string }>
  generated_at: string | null
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
  narrative?: string | null
  intent?: string | null
  hook_idea?: string | null
  strategic_reasoning?: string | null
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
  atracao: 'Atrair',
  connect: 'Conectar',
  conexao: 'Conectar',
  authority: 'Autoridade',
  autoridade: 'Autoridade',
  sell: 'Vender',
  conversao: 'Converter',
  compartilhamento: 'Compartilhar',
  break_objection: 'Quebrar Objeção',
  retention: 'Retenção',
}

export const OBJECTIVE_COLORS: Record<string, string> = {
  attract: 'bg-blue-900/40 text-blue-300 border-blue-700',
  atracao: 'bg-blue-900/40 text-blue-300 border-blue-700',
  connect: 'bg-green-900/40 text-green-300 border-green-700',
  conexao: 'bg-green-900/40 text-green-300 border-green-700',
  authority: 'bg-violet-900/40 text-violet-300 border-violet-700',
  autoridade: 'bg-violet-900/40 text-violet-300 border-violet-700',
  sell: 'bg-orange-900/40 text-orange-300 border-orange-700',
  conversao: 'bg-orange-900/40 text-orange-300 border-orange-700',
  compartilhamento: 'bg-pink-900/40 text-pink-300 border-pink-700',
  break_objection: 'bg-red-900/40 text-red-300 border-red-700',
  retention: 'bg-cyan-900/40 text-cyan-300 border-cyan-700',
}

export const FUNNEL_STAGE_LABELS: Record<string, string> = {
  identificacao: 'Identificação',
  dor: 'Dor',
  autoridade: 'Autoridade',
  quebra_objecao: 'Quebra de Objeção',
  desejo: 'Desejo',
  conversao: 'Conversão',
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
