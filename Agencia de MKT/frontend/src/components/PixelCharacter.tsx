import type { AgentStatus } from '../types'

interface Props {
  status: AgentStatus
  shirtColor: string
  hairColor: string
}

export function PixelCharacter({ status, shirtColor, hairColor }: Props) {
  const isTyping    = status === 'generating'
  const isThinking  = status === 'thinking'
  const isDone      = status === 'completed'
  const isError     = status === 'error'
  const isIdle      = status === 'idle'

  const bodyAnim =
    isTyping   ? 'pc-type 0.35s ease-in-out infinite' :
    isThinking ? 'pc-think 1.2s ease-in-out infinite' :
    isDone     ? 'pc-cheer 0.5s ease-in-out 3' :
    isError    ? 'pc-shake 0.3s ease-in-out 3' :
    'pc-idle 3s ease-in-out infinite'

  const armAnim =
    isTyping ? 'arm-type 0.35s ease-in-out infinite alternate' : 'none'

  const skinColor = '#f5c5a3'
  const pantsColor = '#374151'

  return (
    <svg viewBox="0 0 32 48" className="w-full h-full" style={{ imageRendering: 'pixelated' }}>
      <style>{`
        .pc-body { animation: ${bodyAnim}; transform-origin: 16px 32px; }
        .pc-arm-l { animation: ${armAnim}; transform-origin: 10px 30px; }
        .pc-arm-r { animation: ${armAnim.replace('infinite alternate','infinite')}; transform-origin: 22px 30px; }
        @keyframes pc-idle   { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-1px)} }
        @keyframes pc-think  { 0%,100%{transform:rotate(-3deg)} 50%{transform:rotate(3deg)} }
        @keyframes pc-type   { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-2px)} }
        @keyframes pc-cheer  { 0%{transform:translateY(0) scale(1)} 50%{transform:translateY(-6px) scale(1.1)} 100%{transform:translateY(0) scale(1)} }
        @keyframes pc-shake  { 0%,100%{transform:translateX(0)} 25%{transform:translateX(-3px)} 75%{transform:translateX(3px)} }
        @keyframes arm-type  { 0%{transform:rotate(-20deg) translateY(0)} 100%{transform:rotate(10deg) translateY(-2px)} }
      `}</style>

      <g className="pc-body">
        {/* Shadow */}
        <ellipse cx="16" cy="47" rx="8" ry="2" fill="#000" opacity="0.15"/>

        {/* Legs */}
        <rect x="11" y="37" width="4" height="8" rx="2" fill={pantsColor}/>
        <rect x="17" y="37" width="4" height="8" rx="2" fill={pantsColor}/>
        {/* Shoes */}
        <rect x="10" y="43" width="5" height="3" rx="1.5" fill="#1f2937"/>
        <rect x="17" y="43" width="5" height="3" rx="1.5" fill="#1f2937"/>

        {/* Body / shirt */}
        <rect x="9" y="24" width="14" height="14" rx="3" fill={shirtColor}/>

        {/* Arms */}
        <rect x="4"  y="25" width="5" height="10" rx="2.5" fill={shirtColor} className="pc-arm-l"/>
        <rect x="23" y="25" width="5" height="10" rx="2.5" fill={shirtColor} className="pc-arm-r"/>
        {/* Hands */}
        <circle cx="6.5"  cy="36" r="2.5" fill={skinColor} className="pc-arm-l"/>
        <circle cx="25.5" cy="36" r="2.5" fill={skinColor} className="pc-arm-r"/>

        {/* Neck */}
        <rect x="14" y="20" width="4" height="5" rx="2" fill={skinColor}/>

        {/* Head */}
        <rect x="9" y="8" width="14" height="14" rx="5" fill={skinColor}/>

        {/* Hair */}
        <rect x="9"  y="8"  width="14" height="5"  rx="4" fill={hairColor}/>
        <rect x="9"  y="8"  width="4"  height="10" rx="3" fill={hairColor}/>
        <rect x="19" y="8"  width="4"  height="10" rx="3" fill={hairColor}/>

        {/* Eyes */}
        {isDone ? (
          <>
            <path d="M12 17 Q14 14 16 17" fill="none" stroke="#92400e" strokeWidth="1.2" strokeLinecap="round"/>
            <path d="M16 17 Q18 14 20 17" fill="none" stroke="#92400e" strokeWidth="1.2" strokeLinecap="round"/>
          </>
        ) : isError ? (
          <>
            <line x1="11" y1="15" x2="14" y2="18" stroke="#dc2626" strokeWidth="1.2" strokeLinecap="round"/>
            <line x1="14" y1="15" x2="11" y2="18" stroke="#dc2626" strokeWidth="1.2" strokeLinecap="round"/>
            <line x1="18" y1="15" x2="21" y2="18" stroke="#dc2626" strokeWidth="1.2" strokeLinecap="round"/>
            <line x1="21" y1="15" x2="18" y2="18" stroke="#dc2626" strokeWidth="1.2" strokeLinecap="round"/>
          </>
        ) : (
          <>
            <circle cx="13.5" cy="16" r="1.8" fill="#1f2937"/>
            <circle cx="18.5" cy="16" r="1.8" fill="#1f2937"/>
            <circle cx="14"   cy="15.5" r="0.7" fill="white"/>
            <circle cx="19"   cy="15.5" r="0.7" fill="white"/>
            {isThinking && <circle cx="16" cy="14" r="0.8" fill="#60a5fa" style={{animation:'ping 1s infinite'}}/>}
          </>
        )}

        {/* Mouth */}
        {isDone
          ? <path d="M13 20 Q16 22.5 19 20" fill="none" stroke="#92400e" strokeWidth="1" strokeLinecap="round"/>
          : isError
          ? <path d="M13 21 Q16 19 19 21" fill="none" stroke="#92400e" strokeWidth="1" strokeLinecap="round"/>
          : <rect x="13.5" y="20" width="5" height="1.2" rx="0.6" fill="#92400e" opacity="0.5"/>}

        {/* Thinking bubble */}
        {isThinking && (
          <g style={{animation:'float-dot 1s ease-in-out infinite'}}>
            <circle cx="23" cy="7" r="1"   fill="white" opacity="0.8"/>
            <circle cx="26" cy="5" r="1.5" fill="white" opacity="0.8"/>
            <circle cx="29" cy="2" r="2"   fill="white" opacity="0.7"/>
          </g>
        )}

        {/* Done stars */}
        {isDone && (
          <g style={{animation:'sparkle-pop 0.8s ease-out forwards'}}>
            <text x="0"  y="6"  fontSize="6">⭐</text>
            <text x="24" y="6"  fontSize="6">⭐</text>
          </g>
        )}
      </g>
    </svg>
  )
}
