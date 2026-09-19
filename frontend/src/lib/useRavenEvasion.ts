import { useEffect, type RefObject } from 'react'
import { cursor } from './cursor'
import { prefersReducedMotion } from './warp'

const REPULSION_RADIUS = 380 // px from Raven's chest where she starts to notice you
const MAX_DISPLACEMENT = 38 // px — enough to read as evasion, small enough to stay composed
const MAX_ROTATION = 3.5 // deg of lean away from the pointer
const LERP = 0.038 // heavy, dreamy easing; she drifts back rather than snapping

interface Targets {
  figure: RefObject<HTMLElement | null>
  wrapper: RefObject<HTMLElement | null>
  shadow: RefObject<HTMLElement | null>
}

/**
 * Raven drifts away from the pointer and leans as she goes, with the ground shadow trailing
 * at a fraction of the displacement for parallax. The crack behind her does not move: she
 * is guarding it, so she moves around it.
 *
 * `active` is false during the warp so the physics loop is not fighting the CSS zoom for
 * the same transform property mid-jump.
 */
export function useRavenEvasion(refs: Targets, active: boolean): void {
  useEffect(() => {
    if (!active || prefersReducedMotion()) return

    let tx = 0
    let ty = 0
    let trot = 0
    let x = 0
    let y = 0
    let rot = 0
    let frame = 0

    const step = () => {
      const figure = refs.figure.current
      const wrapper = refs.wrapper.current
      if (!figure || !wrapper) {
        frame = requestAnimationFrame(step)
        return
      }

      const rect = figure.getBoundingClientRect()
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height * 0.42 // chest / orb line, not the bounding-box centre

      tx = 0
      ty = 0
      trot = 0

      if (cursor.inside) {
        const dx = cx - cursor.x // vector pointing AWAY from the pointer
        const dy = cy - cursor.y
        const dist = Math.hypot(dx, dy)
        if (dist < REPULSION_RADIUS && dist > 0.1) {
          const near = 1 - dist / REPULSION_RADIUS
          // Pow > 1 keeps the outer edge of the radius almost inert, so the evasion feels
          // like a reaction to closeness rather than a field she is always sitting in.
          const push = Math.pow(near, 1.8) * MAX_DISPLACEMENT
          tx = (dx / dist) * push
          ty = (dy / dist) * push
          trot = -(dx / dist) * near * MAX_ROTATION
        }
      }

      x += (tx - x) * LERP
      y += (ty - y) * LERP
      rot += (trot - rot) * LERP

      wrapper.style.transform = `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) rotate(${rot.toFixed(2)}deg)`

      const shadow = refs.shadow.current
      if (shadow) {
        shadow.style.transform = `translate3d(${(-x * 0.35).toFixed(2)}px, 0, 0) scale(${(1 - Math.abs(y) / 320).toFixed(3)})`
      }
      frame = requestAnimationFrame(step)
    }

    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [refs, active])
}
