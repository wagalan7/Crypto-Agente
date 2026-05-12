export interface ProductInput {
  produto: string
  preco: string
  publico: string
  objetivo: string
  plataforma: string
  tom_de_voz: string
  pagina_vendas?: string
  orcamento?: string  // orçamento total disponível para mídia paga
}

export type AgentStatus = 'idle' | 'thinking' | 'generating' | 'publishing' | 'completed' | 'error'

export interface AgentState {
  status: AgentStatus
  task: string
  progress: number
  logs: string[]
  output: string
}

export type AgentName =
  | 'ESTRATEGIA' | 'COPY' | 'DESIGN' | 'VIDEO'
  | 'SOCIAL' | 'ADS' | 'AUTOMACAO' | 'PUBLICADOR' | 'ANALYTICS' | 'REVISOR'

export interface AgentMeta {
  label: string
  icon: string
  color: string
  glow: string
  phase: number
}

export const AGENTS: Record<AgentName, AgentMeta> = {
  ESTRATEGIA: { label: 'Estratégia',  icon: '◈', color: 'from-violet-600 to-violet-800',   glow: 'shadow-violet-500/40',  phase: 1 },
  COPY:        { label: 'Copy',        icon: '✦', color: 'from-blue-600 to-blue-800',       glow: 'shadow-blue-500/40',    phase: 2 },
  DESIGN:      { label: 'Design',      icon: '◉', color: 'from-pink-600 to-pink-800',       glow: 'shadow-pink-500/40',    phase: 2 },
  VIDEO:       { label: 'Vídeo',       icon: '▶', color: 'from-red-600 to-red-800',         glow: 'shadow-red-500/40',     phase: 2 },
  SOCIAL:      { label: 'Social',      icon: '◎', color: 'from-cyan-600 to-cyan-800',       glow: 'shadow-cyan-500/40',    phase: 3 },
  ADS:         { label: 'Ads',         icon: '◆', color: 'from-amber-600 to-amber-800',     glow: 'shadow-amber-500/40',   phase: 3 },
  AUTOMACAO:   { label: 'Automação',   icon: '⟳', color: 'from-orange-600 to-orange-800',   glow: 'shadow-orange-500/40',  phase: 3 },
  PUBLICADOR:  { label: 'Publicador',  icon: '↑', color: 'from-emerald-600 to-emerald-800', glow: 'shadow-emerald-500/40', phase: 4 },
  ANALYTICS:   { label: 'Analytics',  icon: '◐', color: 'from-teal-600 to-teal-800',       glow: 'shadow-teal-500/40',    phase: 5 },
  REVISOR:     { label: 'Revisor',    icon: '✔', color: 'from-lime-600 to-lime-800',        glow: 'shadow-lime-500/40',    phase: 6 },
}

export const STATUS_COLOR: Record<AgentStatus, string> = {
  idle:       'text-gray-600',
  thinking:   'text-blue-400',
  generating: 'text-purple-400',
  publishing: 'text-orange-400',
  completed:  'text-emerald-400',
  error:      'text-red-400',
}

export const STATUS_LABEL: Record<AgentStatus, string> = {
  idle:       'aguardando',
  thinking:   'pensando...',
  generating: 'gerando...',
  publishing: 'publicando...',
  completed:  'concluído',
  error:      'erro',
}

export interface SSEAgentEvent {
  type: 'agent_event'
  payload: { agent: AgentName; status: AgentStatus; task: string; progress: number; logs: string[] }
}

export interface SSEChunkEvent {
  type: 'chunk'
  payload: { agent: AgentName; text: string }
}

export interface SSEStatusEvent {
  type: 'status'
  payload: string
}

export interface SSEDoneEvent {
  type: 'done'
  payload: string
}

export interface SSEKeepaliveEvent {
  type: 'keepalive'
  payload: string
}

export type SSEEvent = SSEAgentEvent | SSEChunkEvent | SSEStatusEvent | SSEDoneEvent | SSEKeepaliveEvent
