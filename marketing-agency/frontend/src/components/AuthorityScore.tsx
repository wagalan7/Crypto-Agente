interface Props {
  score: number
}

function getColor(score: number) {
  if (score >= 75) return { ring: 'stroke-green-500', text: 'text-green-400', label: 'Alta Autoridade' }
  if (score >= 50) return { ring: 'stroke-violet-500', text: 'text-violet-400', label: 'Em Crescimento' }
  if (score >= 25) return { ring: 'stroke-amber-500', text: 'text-amber-400', label: 'Iniciando' }
  return { ring: 'stroke-gray-600', text: 'text-gray-400', label: 'Sem Dados' }
}

export function AuthorityScore({ score }: Props) {
  const { ring, text, label } = getColor(score)
  const circumference = 2 * Math.PI * 40
  const dashoffset = circumference * (1 - score / 100)

  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative w-24 h-24">
        <svg className="w-24 h-24 -rotate-90" viewBox="0 0 100 100">
          <circle cx="50" cy="50" r="40" fill="none" stroke="#1f2937" strokeWidth="8" />
          <circle
            cx="50" cy="50" r="40" fill="none"
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashoffset}
            className={`${ring} transition-all duration-700`}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={`text-2xl font-bold ${text}`}>{score.toFixed(0)}</span>
        </div>
      </div>
      <div className="text-center">
        <p className={`text-xs font-semibold ${text}`}>{label}</p>
        <p className="text-xs text-gray-500">Score de Autoridade</p>
      </div>
    </div>
  )
}
