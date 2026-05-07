import { useState, useCallback } from 'react'
import { ProductForm } from './components/ProductForm'
import { AgentSection } from './components/AgentSection'
import { StatusBar } from './components/StatusBar'
import type { ProductInput, AgencyState, SSEEvent, SectionKey } from './types'
import { SECTION_META } from './types'

const SECTIONS = Object.keys(SECTION_META) as SectionKey[]

const emptyState = (): AgencyState => ({
  estrategia: '', copy: '', conteudo: '', criativos: '', ads: '', automacao: '', publicacao: '',
})

export default function App() {
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState('')
  const [output, setOutput] = useState<AgencyState>(emptyState())
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null)
  const [doneSections, setDoneSections] = useState<Set<SectionKey>>(new Set())
  const [finished, setFinished] = useState(false)

  const handleSubmit = useCallback(async (data: ProductInput) => {
    setLoading(true)
    setOutput(emptyState())
    setDoneSections(new Set())
    setActiveSection(null)
    setFinished(false)
    setStatus('Iniciando agência...')

    try {
      const res = await fetch('/agency/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })

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
          const event: SSEEvent = JSON.parse(line.slice(6))

          if (event.type === 'status') {
            setStatus(event.payload as string)
          } else if (event.type === 'chunk') {
            const { section, text } = event.payload as { section: SectionKey; text: string }
            setActiveSection(section)
            setOutput(prev => ({ ...prev, [section]: prev[section] + text }))
          } else if (event.type === 'section_done') {
            const sec = event.payload as SectionKey
            setDoneSections(prev => new Set([...prev, sec]))
            setActiveSection(null)
          } else if (event.type === 'done') {
            setStatus(event.payload as string)
            setFinished(true)
            setLoading(false)
          }
        }
      }
    } catch (err) {
      setStatus('Erro ao conectar com a agência. Verifique o backend.')
      setLoading(false)
    }
  }, [])

  return (
    <div className="min-h-screen bg-gray-950">
      <header className="border-b border-gray-800 px-6 py-4">
        <div className="max-w-4xl mx-auto flex items-center gap-3">
          <div className="w-8 h-8 bg-brand-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">A</div>
          <div>
            <h1 className="text-base font-bold text-white leading-none">Agência de Marketing IA</h1>
            <p className="text-xs text-gray-500 mt-0.5">7 Agentes • Automação Completa</p>
          </div>
          {finished && (
            <span className="ml-auto text-xs text-green-400 bg-green-900/30 border border-green-700 px-3 py-1 rounded-full">
              Campanha gerada
            </span>
          )}
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-6 space-y-4">
        <ProductForm onSubmit={handleSubmit} loading={loading} />

        {status && <StatusBar message={status} loading={loading} />}

        {SECTIONS.map(key => (
          <AgentSection
            key={key}
            sectionKey={key}
            text={output[key]}
            active={activeSection === key}
            done={doneSections.has(key)}
          />
        ))}
      </main>
    </div>
  )
}
