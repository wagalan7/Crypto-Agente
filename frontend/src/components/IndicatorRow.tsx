interface Props {
  label: string
  value: string | number | null | undefined
  colorClass?: string
}

export function IndicatorRow({ label, value, colorClass = 'text-slate-300' }: Props) {
  if (value == null) return null
  return (
    <div className="flex justify-between items-center py-0.5">
      <span className="text-xs text-slate-500">{label}</span>
      <span className={`text-xs font-mono font-semibold ${colorClass}`}>{value}</span>
    </div>
  )
}
