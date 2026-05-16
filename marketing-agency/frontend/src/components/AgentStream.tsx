import { useState, useRef } from 'react'

interface SavePayload {
  title: string
  format: string
  platform: string
  objective: string
  field?: 'script' | 'copy' | 'design_brief' | 'strategic_note'
}

interface Props {
  label: string
  onRun: () => AsyncGenerator<{ type: string; payload: string }>
  placeholder?: string
  onSave?: (output: string) => Promise<SavePayload | null> | SavePayload | null
  saveLabel?: string
}

export function AgentStream({ label, onRun, placeholder, onSave, saveLabel = 'Salvar como Conteúdo' }: Props) {
  const [output, setOutput] = useState('')
  const [status, setStatus] = useState('')
  const [running, setRunning] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const abortRef = useRef(false)

  async function run() {
    setRunning(true)
    setOutput('')
    setStatus('')
    setSaveMsg('')
    abortRef.current = false

    try {
      const gen = onRun()
      for await (const event of gen) {
        if (abortRef.current) break
        if (event.type === 'status') setStatus(event.payload)
        else if (event.type === 'chunk') setOutput(prev => prev + event.payload)
        else if (event.type === 'done') setStatus(event.payload)
      }
    } catch (e: any) {
      setStatus(`Erro: ${e.message}`)
    } finally {
      setRunning(false)
    }
  }

  async function handleSave() {
    if (!onSave || !output) return
    setSaving(true)
    setSaveMsg('')
    try {
      const result = await onSave(output)
      if (result) setSaveMsg('Salvo! Veja na aba Conteúdo.')
    } catch (e: any) {
      setSaveMsg(`Erro ao salvar: ${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        {status && (
          <p className="text-xs text-gray-400 flex items-center gap-1.5">
            {running && <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />}
            {status}
          </p>
        )}
        <button
          onClick={run}
          disabled={running}
          className="btn-primary ml-auto"
        >
          {running ? 'Gerando...' : label}
        </button>
      </div>

      {output ? (
        <div className="card max-h-96 overflow-y-auto">
          <pre className={`agent-output${running ? ' blinking-cursor' : ''}`}>{output}</pre>
        </div>
      ) : !running && placeholder ? (
        <div className="card text-center py-8">
          <p className="text-gray-500 text-sm">{placeholder}</p>
        </div>
      ) : null}

      {output && !running && onSave && (
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-gray-500">{saveMsg}</p>
          <button onClick={handleSave} disabled={saving} className="btn-secondary text-xs">
            {saving ? 'Salvando...' : saveLabel}
          </button>
        </div>
      )}
    </div>
  )
}
