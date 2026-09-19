import { useEffect, useRef } from 'react'
import { cursor } from '../lib/cursor'
import { prefersReducedMotion } from '../lib/warp'
import { scene } from '../lib/scene'

const CONNECT_RADIUS = 165 // how close a particle must be to the cursor to link to it
const NEIGHBOUR_RADIUS = 130 // and to each other, once both are inside the cursor's sphere
const HUES = [
  'rgba(216, 180, 254,',
  'rgba(192, 132, 252,',
  'rgba(244, 114, 182,',
  'rgba(168, 85, 247,',
] as const

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  alpha: number
  twinkle: number
  phase: number
  hue: string
}

/** Halo radius as a multiple of the particle's core radius. */
const HALO = 2.3
/**
 * Stars near the crack are alive; stars out at the edges are just there. `nearness` is 1 at
 * the viewport centre (where the rift sits) and falls to 0 toward the corners, and it scales
 * brightness, halo, twinkle and drift together so the eye is pulled inward rather than
 * flicked around the frame.
 */
const QUIET_FROM = 0.3 // normalised distance where the falloff starts
const QUIET_TO = 1.05 // …and where it bottoms out
const QUIET_FLOOR = 0.42 // how much activity the farthest stars keep

/**
 * One pre-rendered glow per hue: a bright core fading to nothing. Drawn with drawImage,
 * which on an accelerated canvas is a textured quad — the cheapest thing it can do.
 *
 * This replaces `shadowBlur` on every particle fill. A canvas shadow is not a cheap
 * primitive: each shadowed fill allocates a scratch surface, blurs it, and composites it
 * back, so ~110 of them was ~110 blur passes per frame — the single largest cost on the
 * idle landing page.
 */
function makeSprites(): Map<string, HTMLCanvasElement> {
  const sprites = new Map<string, HTMLCanvasElement>()
  const size = 48
  for (const hue of HUES) {
    const c = document.createElement('canvas')
    c.width = size
    c.height = size
    const g = c.getContext('2d')
    if (!g) continue
    const half = size / 2
    const grad = g.createRadialGradient(half, half, 0, half, half, half)
    grad.addColorStop(0, `${hue} 1)`)
    grad.addColorStop(0.18, `${hue} 0.85)`)
    grad.addColorStop(0.42, `${hue} 0.14)`)
    grad.addColorStop(1, `${hue} 0)`)
    g.fillStyle = grad
    g.fillRect(0, 0, size, size)
    sprites.set(hue, c)
  }
  return sprites
}

/**
 * Drifting starfield that grows a constellation around the pointer.
 *
 * The neighbour pass is O(n²) in the worst case, so it only runs for particles already
 * inside the cursor radius — at ~110 particles that keeps the inner loop to a handful of
 * pairs per frame instead of six thousand.
 */
interface Props {
  /** 1 is the landing's starfield against a black wall. The options page runs above 1: its
      collage swallows faint stars, so they need more alpha to read the same. Scales
      brightness, count and the cursor web together. */
  intensity?: number
  /** scene.covered switches the landing's loops off while the about panel is over it. The
      about page's own sky lives under that same flag, so it opts out of the pause and keeps
      drawing. */
  alwaysOn?: boolean
}

export default function ConstellationCanvas({ intensity = 1, alwaysOn = false }: Props) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const calm = prefersReducedMotion()
    const sprites = makeSprites()
    let w = 0
    let h = 0
    let dpr = 1
    let particles: Particle[] = []

    const spawn = (initial: boolean): Particle => {
      const speed = calm ? 0.08 : 0.15 + Math.random() * 0.35
      const angle = Math.random() * Math.PI * 2
      return {
        x: Math.random() * w,
        y: initial ? Math.random() * h : Math.random() < 0.5 ? -5 : h + 5,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        r: 0.9 + Math.random() * 1.2,
        alpha: 0.1 + Math.random() * 0.3,
        twinkle: 0.008 + Math.random() * 0.015,
        phase: Math.random() * Math.PI * 2,
        hue: HUES[Math.floor(Math.random() * HUES.length)] as string,
      }
    }

    const resize = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = window.innerWidth
      h = window.innerHeight
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

      const count = Math.round(Math.min(110, Math.max(75, Math.floor((w * h) / 14000))) * (0.6 + 0.4 * intensity))
      if (particles.length === 0) {
        particles = Array.from({ length: count }, () => spawn(true))
      } else if (particles.length < count) {
        while (particles.length < count) particles.push(spawn(true))
      } else {
        particles.length = count
      }
    }

    resize()
    window.addEventListener('resize', resize)

    let frame = 0
    const render = () => {
      if (!alwaysOn && scene.covered) {
        frame = requestAnimationFrame(render)
        return
      }
      ctx.clearRect(0, 0, w, h)

      const cx = w / 2
      const cy = h / 2
      for (const p of particles) {
        // 0 at the crack, ~1 at the middle of an edge, ~1.4 in a corner.
        const dn = Math.hypot((p.x - cx) / cx, (p.y - cy) / cy)
        const u = Math.min(1, Math.max(0, (dn - QUIET_FROM) / (QUIET_TO - QUIET_FROM)))
        const near = 1 - u * u * (3 - 2 * u) // smoothstep
        const act = QUIET_FLOOR + (1 - QUIET_FLOOR) * near
        p.x += p.vx * act
        p.y += p.vy * act
        p.phase += p.twinkle * act
        if (p.x < -20) p.x = w + 20
        else if (p.x > w + 20) p.x = -20
        if (p.y < -20) p.y = h + 20
        else if (p.y > h + 20) p.y = -20

        const a = Math.max(0.02, Math.min(0.5, (p.alpha + Math.sin(p.phase) * 0.1 * act) * act)) * intensity
        const sprite = sprites.get(p.hue)
        if (!sprite) continue
        const d = p.r * HALO * 2 * (0.8 + 0.2 * act)
        ctx.globalAlpha = a
        ctx.drawImage(sprite, p.x - d / 2, p.y - d / 2, d, d)
      }
      ctx.globalAlpha = 1

      if (cursor.inside) {
        const { x: mx, y: my } = cursor
        for (let i = 0; i < particles.length; i++) {
          const p1 = particles[i] as Particle
          const d1 = Math.hypot(p1.x - mx, p1.y - my)
          if (d1 >= CONNECT_RADIUS) continue

          const pull = 1 - d1 / CONNECT_RADIUS
          ctx.beginPath()
          ctx.moveTo(p1.x, p1.y)
          ctx.lineTo(mx, my)
          ctx.strokeStyle = `rgba(216, 180, 254, ${(pull * 0.22 * intensity).toFixed(3)})`
          ctx.lineWidth = 0.75
          ctx.stroke()

          for (let j = i + 1; j < particles.length; j++) {
            const p2 = particles[j] as Particle
            const gap = Math.hypot(p1.x - p2.x, p1.y - p2.y)
            if (gap >= NEIGHBOUR_RADIUS) continue
            const d2 = Math.hypot(p2.x - mx, p2.y - my)
            if (d2 >= CONNECT_RADIUS) continue

            const link =
              (1 - gap / NEIGHBOUR_RADIUS) * Math.min(pull, 1 - d2 / CONNECT_RADIUS)
            ctx.beginPath()
            ctx.moveTo(p1.x, p1.y)
            ctx.lineTo(p2.x, p2.y)
            ctx.strokeStyle = `rgba(192, 132, 252, ${(link * 0.18 * intensity).toFixed(3)})`
            ctx.lineWidth = 0.65
            ctx.stroke()
          }
        }
      }

      frame = requestAnimationFrame(render)
    }

    frame = requestAnimationFrame(render)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', resize)
    }
  }, [intensity, alwaysOn])

  return <canvas className="constellation" ref={ref} aria-hidden="true" />
}
