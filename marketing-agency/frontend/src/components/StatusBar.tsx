interface Props {
  message: string
  loading: boolean
}

export function StatusBar({ message, loading }: Props) {
  if (!message) return null
  return (
    <div className="flex items-center gap-3 px-4 py-3 bg-gray-900 border border-gray-700 rounded-xl text-sm text-gray-300">
      {loading && (
        <div className="w-4 h-4 border-2 border-brand-500 border-t-transparent rounded-full animate-spin shrink-0" />
      )}
      <span>{message}</span>
    </div>
  )
}
