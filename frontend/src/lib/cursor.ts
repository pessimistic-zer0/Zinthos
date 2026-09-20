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

  const clear = () => {
    cursor.inside = false
    cursor.x = -9999
    cursor.y = -9999
  }

  const onMove = (e: PointerEvent) => {
    // Mouse and pen only. A touch also fires pointermove, and taking it would be wrong in
    // both directions: there is no cursor hovering between taps, so Raven would flinch
    // away from wherever you last touched and STAY there — `pointerleave` does not fire
    // for a finger that simply lifted — and the constellation would keep linking up around
    // a point nobody is pointing at. Both effects answer a pointer that is present; on a
    // touch screen, between taps, none is.
    if (e.pointerType !== 'mouse' && e.pointerType !== 'pen') {
      if (cursor.inside) clear()
      return
    }
    cursor.x = e.clientX
    cursor.y = e.clientY
    cursor.inside = true
  }

  window.addEventListener('pointermove', onMove, { passive: true })
  document.addEventListener('pointerleave', clear)

  return () => {
    window.removeEventListener('pointermove', onMove)
    document.removeEventListener('pointerleave', clear)
    installed = false
  }
}
