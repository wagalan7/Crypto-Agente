import type { AgentStatus } from '../types'

interface Props {
  status: AgentStatus
  accentColor?: string
}

const STATUS_GLOW: Record<AgentStatus, string> = {
  idle:       '#374151',
  thinking:   '#60a5fa',
  generating: '#a855f7',
  publishing: '#fb923c',
  completed:  '#34d399',
  error:      '#f87171',
}

export function Robot({ status, accentColor }: Props) {
  const glow = accentColor ?? STATUS_GLOW[status]
  const isIdle       = status === 'idle'
  const isThinking   = status === 'thinking'
  const isGenerating = status === 'generating'
  const isDone       = status === 'completed'
  const isError      = status === 'error'
  const isActive     = isThinking || isGenerating

  const bodyAnim =
    isThinking   ? 'robot-think' :
    isGenerating ? 'robot-type-body' :
    isDone       ? 'robot-jump' :
    isError      ? 'robot-shake' :
    'robot-idle'

  const eyeColor = isIdle ? '#6b7280' : glow

  return (
    <svg viewBox="0 0 60 86" className="w-full h-full drop-shadow-lg" style={{ filter: isActive ? `drop-shadow(0 0 6px ${glow}88)` : 'none' }}>
      <style>{`
        .rb { animation: ${bodyAnim} 1.8s ease-in-out infinite; transform-origin: 30px 60px; }
        .arm-l { animation: ${isGenerating ? 'arm-type-l' : isIdle ? 'arm-idle-l' : 'none'} ${isGenerating ? '0.4s' : '2.5s'} ease-in-out infinite; transform-origin: 9px 52px; }
        .arm-r { animation: ${isGenerating ? 'arm-type-r' : isIdle ? 'arm-idle-r' : 'none'} ${isGenerating ? '0.4s' : '2.5s'} ease-in-out infinite alternate; transform-origin: 51px 52px; }
        .eye-l { animation: ${isDone ? 'none' : isError ? 'none' : 'eye-blink'} 3s ease-in-out ${isActive ? '0.3s' : '0s'} infinite; transform-origin: 21px 23px; }
        .eye-r { animation: ${isDone ? 'none' : isError ? 'none' : 'eye-blink'} 3s ease-in-out ${isActive ? '0.7s' : '0.5s'} infinite; transform-origin: 39px 23px; }
        .antenna-dot { animation: ${isThinking ? 'antenna-pulse' : isGenerating ? 'antenna-fast' : 'antenna-slow'} ${isGenerating ? '0.6s' : '2s'} ease-in-out infinite; }
        .chest-line { animation: ${isGenerating ? 'chest-scroll' : 'none'} 0.8s linear infinite; }
        .sparkle { animation: sparkle-pop 0.6s ease-out forwards; }
      `}</style>

      <g className="rb">
        {/* Antenna */}
        <line x1="30" y1="9" x2="30" y2="3" stroke="#6b7280" strokeWidth="2" strokeLinecap="round"/>
        <circle cx="30" cy="3" r="2.5" className="antenna-dot" fill={glow}/>

        {/* Head */}
        <rect x="11" y="9" width="38" height="30" rx="8"
          fill="#1f2937" stroke={isActive || isDone ? glow : '#374151'} strokeWidth={isActive ? '1.5' : '1'}/>

        {/* Head shine */}
        <rect x="15" y="11" width="30" height="6" rx="4" fill="white" opacity="0.04"/>

        {/* Eyes */}
        {isDone ? (
          <>
            {/* Happy crescent eyes */}
            <path d="M16 22 Q21 17 26 22" fill="none" stroke={glow} strokeWidth="2.5" strokeLinecap="round" className="eye-l"/>
            <path d="M34 22 Q39 17 44 22" fill="none" stroke={glow} strokeWidth="2.5" strokeLinecap="round" className="eye-r"/>
            {/* Sparkles */}
            <g className="sparkle">
              <line x1="8"  y1="13" x2="8"  y2="17" stroke="#fbbf24" strokeWidth="1.5" strokeLinecap="round"/>
              <line x1="6"  y1="15" x2="10" y2="15" stroke="#fbbf24" strokeWidth="1.5" strokeLinecap="round"/>
              <line x1="52" y1="13" x2="52" y2="17" stroke="#fbbf24" strokeWidth="1.5" strokeLinecap="round"/>
              <line x1="50" y1="15" x2="54" y2="15" stroke="#fbbf24" strokeWidth="1.5" strokeLinecap="round"/>
            </g>
          </>
        ) : isError ? (
          <>
            {/* X eyes */}
            <line x1="17" y1="19" x2="25" y2="27" stroke="#f87171" strokeWidth="2.5" strokeLinecap="round"/>
            <line x1="25" y1="19" x2="17" y2="27" stroke="#f87171" strokeWidth="2.5" strokeLinecap="round"/>
            <line x1="35" y1="19" x2="43" y2="27" stroke="#f87171" strokeWidth="2.5" strokeLinecap="round"/>
            <line x1="43" y1="19" x2="35" y2="27" stroke="#f87171" strokeWidth="2.5" strokeLinecap="round"/>
          </>
        ) : (
          <>
            <circle cx="21" cy="23" r="5" fill={isIdle ? '#374151' : glow} fillOpacity={isIdle ? 1 : 0.9} className="eye-l"/>
            <circle cx="39" cy="23" r="5" fill={isIdle ? '#374151' : glow} fillOpacity={isIdle ? 1 : 0.9} className="eye-r"/>
            {/* Pupil */}
            <circle cx="22" cy="22" r="1.5" fill="white" opacity="0.7"/>
            <circle cx="40" cy="22" r="1.5" fill="white" opacity="0.7"/>
            {/* Thinking dots inside eyes */}
            {isThinking && <>
              <circle cx="21" cy="23" r="2" fill="white" opacity="0.9"/>
              <circle cx="39" cy="23" r="2" fill="white" opacity="0.9"/>
            </>}
          </>
        )}

        {/* Mouth */}
        {isDone
          ? <path d="M22 32 Q30 37 38 32" fill="none" stroke={glow} strokeWidth="2" strokeLinecap="round"/>
          : isError
          ? <path d="M22 36 Q30 31 38 36" fill="none" stroke="#f87171" strokeWidth="2" strokeLinecap="round"/>
          : <rect x="22" y="33" width="16" height="2.5" rx="1.25"
              fill={isGenerating ? glow : '#4b5563'} opacity={isGenerating ? '1' : '0.7'}/>
        }

        {/* Neck */}
        <rect x="26" y="39" width="8" height="5" rx="2" fill="#374151"/>

        {/* Body */}
        <rect x="8" y="44" width="44" height="30" rx="8"
          fill="#111827" stroke={isActive || isDone ? glow : '#1f2937'} strokeWidth={isActive ? '1.5' : '1'}/>

        {/* Chest panel */}
        <rect x="15" y="50" width="30" height="17" rx="4" fill="#0f172a" stroke="#1e293b" strokeWidth="0.5"/>
        {isGenerating && <>
          <rect className="chest-line" x="17" y="53" width="26" height="2" rx="1" fill={glow} opacity="0.9"/>
          <rect x="17" y="57" width="18" height="1.5" rx="0.75" fill={glow} opacity="0.5"/>
          <rect x="17" y="60" width="22" height="1.5" rx="0.75" fill={glow} opacity="0.3"/>
          <rect x="17" y="63" width="14" height="1.5" rx="0.75" fill={glow} opacity="0.2"/>
        </>}
        {isThinking && <>
          <text x="26" y="62" fontSize="10" fill={glow} fontFamily="monospace" textAnchor="middle">?</text>
        </>}
        {isDone && <>
          <path d="M22 60 L27 65 L38 54" fill="none" stroke={glow} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
        </>}
        {isError && <>
          <text x="30" y="62" fontSize="10" fill="#f87171" fontFamily="monospace" textAnchor="middle">!</text>
        </>}
        {isIdle && <>
          <rect x="17" y="55" width="26" height="1" rx="0.5" fill="#1e293b"/>
          <rect x="17" y="58" width="20" height="1" rx="0.5" fill="#1e293b"/>
          <rect x="17" y="61" width="23" height="1" rx="0.5" fill="#1e293b"/>
        </>}

        {/* Arms */}
        <rect x="0" y="45" width="8" height="20" rx="4" fill="#1f2937" stroke="#374151" strokeWidth="0.5" className="arm-l"/>
        <rect x="52" y="45" width="8" height="20" rx="4" fill="#1f2937" stroke="#374151" strokeWidth="0.5" className="arm-r"/>
        {/* Hands */}
        <circle cx="4"  cy="66" r="4" fill="#374151" className="arm-l"/>
        <circle cx="56" cy="66" r="4" fill="#374151" className="arm-r"/>

        {/* Legs */}
        <rect x="15" y="74" width="12" height="11" rx="5" fill="#1f2937" stroke="#374151" strokeWidth="0.5"/>
        <rect x="33" y="74" width="12" height="11" rx="5" fill="#1f2937" stroke="#374151" strokeWidth="0.5"/>
        {/* Feet */}
        <rect x="13" y="81" width="16" height="5" rx="3" fill="#374151"/>
        <rect x="31" y="81" width="16" height="5" rx="3" fill="#374151"/>

        {/* Thinking floating dots */}
        {isThinking && <>
          <circle cx="46" cy="10" r="2" fill={glow} opacity="0.9" style={{animation:'float-dot 1s ease-in-out 0s infinite'}}/>
          <circle cx="51" cy="7"  r="1.5" fill={glow} opacity="0.7" style={{animation:'float-dot 1s ease-in-out 0.3s infinite'}}/>
          <circle cx="55" cy="4"  r="1" fill={glow} opacity="0.5" style={{animation:'float-dot 1s ease-in-out 0.6s infinite'}}/>
        </>}
      </g>
    </svg>
  )
}
