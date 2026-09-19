/**
 * Shared pointer position.
 *
 * Deliberately a mutable singleton rather than React state: the constellation canvas and
 * Raven's evasion both sample it every animation frame, and routing pointermove through
 * setState would re-render the whole tree at 60fps for a value nothing renders directly.
 */
export interface CursorState {
  x: number
  y: number
  inside: boolean
}

export const cursor: CursorState = { x: -9999, y: -9999, inside: false }

let installed = false

export function trackCursor(): () => void {
  if (installed) return () => {}
  installed = true

  const onMove = (e: PointerEvent) => {
    cursor.x = e.clientX
    cursor.y = e.clientY
    cursor.inside = true
  }
  const onLeave = () => {
    cursor.inside = false
    cursor.x = -9999
    cursor.y = -9999
  }

  window.addEventListener('pointermove', onMove, { passive: true })
  document.addEventListener('pointerleave', onLeave)

  return () => {
    window.removeEventListener('pointermove', onMove)
    document.removeEventListener('pointerleave', onLeave)
    installed = false
  }
}
