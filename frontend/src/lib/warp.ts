/**
 * Warp timing helpers.
 *
 * The duration lives in CSS (`--warp-duration` in global.css) because the zoom itself is a
 * CSS transition; JS reads it back rather than keeping a second copy that can drift out of
 * step and leave the streak canvas running after the flash has already landed.
 */

const FALLBACK_MS = 950
const CLOSE_FALLBACK_MS = 760

/** Parse a CSS time token ("1250ms" / "1.25s") into milliseconds. */
export function parseCssTime(value: string): number | null {
  const v = value.trim()
  if (v.endsWith('ms')) {
    const n = Number.parseFloat(v)
    return Number.isFinite(n) ? n : null
  }
  if (v.endsWith('s')) {
    const n = Number.parseFloat(v)
    return Number.isFinite(n) ? n * 1000 : null
  }
  return null
}

function cssTimeMs(name: string, fallback: number): number {
  if (typeof window === 'undefined') return fallback
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name)
  return parseCssTime(raw) ?? fallback
}

export function warpDurationMs(): number {
  return cssTimeMs('--warp-duration', FALLBACK_MS)
}

/** How long the tear takes to seal on the way back out. */
export function riftCloseMs(): number {
  return cssTimeMs('--rift-close-duration', CLOSE_FALLBACK_MS)
}

export function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}
