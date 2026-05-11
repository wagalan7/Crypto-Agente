interface Props {
  size?: number
  className?: string
}

export function MagaLogo({ size = 32, className = '' }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <rect width="100" height="100" rx="16" fill="#3A3591" />
      <text
        x="50"
        y="41"
        fontFamily="'Arial Rounded MT Bold','Nunito','Varela Round',Arial,sans-serif"
        fontSize="36"
        fontWeight="900"
        fill="#F0EFF8"
        textAnchor="middle"
        dominantBaseline="middle"
        letterSpacing="-1"
      >
        MA
      </text>
      <text
        x="50"
        y="72"
        fontFamily="'Arial Rounded MT Bold','Nunito','Varela Round',Arial,sans-serif"
        fontSize="36"
        fontWeight="900"
        fill="#F0EFF8"
        textAnchor="middle"
        dominantBaseline="middle"
        letterSpacing="-1"
      >
        GA
      </text>
    </svg>
  )
}

/** Data URI for use as favicon */
export const MAGA_FAVICON = `data:image/svg+xml,${encodeURIComponent(`
<svg width="32" height="32" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <rect width="100" height="100" rx="16" fill="#3A3591"/>
  <text x="50" y="41" font-family="Arial,sans-serif" font-size="36" font-weight="900" fill="#F0EFF8" text-anchor="middle" dominant-baseline="middle">MA</text>
  <text x="50" y="72" font-family="Arial,sans-serif" font-size="36" font-weight="900" fill="#F0EFF8" text-anchor="middle" dominant-baseline="middle">GA</text>
</svg>
`)}`
