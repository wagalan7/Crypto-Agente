import { useState, useRef } from 'react'

interface Props {
  label: string
  onRun: () => AsyncGenerator<{ type: string; payload: string }>
  placeholder?: string
}

export function AgentStream({ label, onRun, placeholder }: Props) {
  const [output, setOutput] = useState('')
  const [status, setStatus] = useState('')
  const [running, setRunning] = useState(false)
  const abortRef = useRef(false)

  async function run() {
    setRunning(true)
    setOutput('')
    setStatus('')
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
    </div>
  )
}
