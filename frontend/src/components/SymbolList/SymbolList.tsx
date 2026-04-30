import { useState } from 'react'
import { Search } from 'lucide-react'

interface Props {
  symbols: string[]
  selected: string
  onSelect: (symbol: string) => void
}

function cleanName(s: string) {
  return s.replace('/USDT:USDT', '/USDT').replace(':USDT', '')
}

export function SymbolList({ symbols, selected, onSelect }: Props) {
  const [search, setSearch] = useState('')

  const filtered = symbols.filter(s =>
    cleanName(s).toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="flex flex-col h-full">
      <div className="relative mb-2">
        <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
        <input
          type="text"
          placeholder="Buscar..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full bg-slate-800 text-slate-200 text-xs pl-7 pr-2 py-2 rounded-lg border border-slate-700 focus:outline-none focus:border-slate-500 placeholder-slate-600"
        />
      </div>
      <div className="flex-1 overflow-y-auto">
        {filtered.map(sym => (
          <button
            key={sym}
            onClick={() => onSelect(sym)}
            className={`w-full text-left px-2 py-1.5 rounded text-xs font-mono transition-colors ${
              selected === sym
                ? 'bg-blue-600/30 text-blue-300 border border-blue-500/30'
                : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
            }`}
          >
            {cleanName(sym)}
          </button>
        ))}
      </div>
    </div>
  )
}
