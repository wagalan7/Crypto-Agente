export interface ProductInput {
  produto: string
  preco: string
  publico: string
  objetivo: string
  plataforma: string
  tom_de_voz: string
}

export type SectionKey = 'estrategia' | 'copy' | 'conteudo' | 'criativos' | 'ads' | 'automacao' | 'publicacao'

export interface AgencyState {
  estrategia: string
  copy: string
  conteudo: string
  criativos: string
  ads: string
  automacao: string
  publicacao: string
}

export interface SSEEvent {
  type: 'status' | 'chunk' | 'section_done' | 'done'
  payload: string | { section: SectionKey; text: string }
}

export const SECTION_META: Record<SectionKey, { label: string; color: string; agent: string }> = {
  estrategia: { label: 'ESTRATÉGIA', color: 'text-violet-400 bg-violet-900/40 border-violet-700', agent: 'Agente 1 — Estrategista' },
  copy:        { label: 'COPY',       color: 'text-blue-400 bg-blue-900/40 border-blue-700',     agent: 'Agente 2 — Copywriter' },
  conteudo:    { label: 'CONTEÚDO',   color: 'text-cyan-400 bg-cyan-900/40 border-cyan-700',     agent: 'Agente 3 — Social Media' },
  criativos:   { label: 'CRIATIVOS',  color: 'text-emerald-400 bg-emerald-900/40 border-emerald-700', agent: 'Agente 4 — Design Diretor' },
  ads:         { label: 'ADS',        color: 'text-amber-400 bg-amber-900/40 border-amber-700',  agent: 'Agente 5 — Tráfego Pago' },
  automacao:   { label: 'AUTOMAÇÃO',  color: 'text-orange-400 bg-orange-900/40 border-orange-700', agent: 'Agente 6 — Automação' },
  publicacao:  { label: 'PUBLICAÇÃO', color: 'text-rose-400 bg-rose-900/40 border-rose-700',     agent: 'Agente 7 — Publicador' },
}
