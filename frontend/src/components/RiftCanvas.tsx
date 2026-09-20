import { useEffect, useRef, type RefObject } from 'react'
import {
  BRANCHES,
  branchPoints,
  randomTendril,
  layoutBoxIn, riftFromLayout,
  riftPolygon,
  tracePath,
  type Branch,
  type RiftGeometry,
} from '../lib/rift'
import { scene } from '../lib/scene'

const MOTE_COUNT = 30
/** Fragments per chipped letter, drifting from the chip to the seam. */
const SHARDS_PER_CHIP = 4
/** Tendrils per second, on average. Rare: a tear, not a live wire. */
const TENDRIL_RATE = 0.35
/** One dim pulse travelling along the rim, and how long it takes end to end. */
const SURGE_COUNT = 1
const SURGE_PERIOD_MS = 5600
/**
 * Where the orbs sit in the hero image, as fractions of its box, and where on the frame each
 * hand's threads land: [x fraction, y fraction] of the frame for the left hand; the right
 * hand mirrors them.
 */
const ORB = { x: 0.15, y: 0.11 }
const ANCHORS: Array<[number, number]> = [
  [0.2, 0],
  [0, 0.14],
  [0, 0.56],
]

interface Mote {
  angle: number
  /** Distance from the seam in units of len; drifts toward 0. */
  dist: number
  speed: number
  size: number
}

interface Shard {
  sx: number
  sy: number
  tx: number
  ty: number
  ink: string
  /** Progress source→seam; negative is a wait before (re)appearing. */
  t: number
  /** Progress per ms. */
  speed: number
  size: number
  rot: number
  spin: number
  wobble: number
}

interface Tendril {
  branch: Branch
  born: number
  life: number
}

interface Props {
  stage: RefObject<HTMLElement | null>
  figure: RefObject<HTMLImageElement | null>
  /** Called whenever the crack's geometry is (re)measured, so the launch uses the same one. */
  onGeometry: (g: RiftGeometry) => void
}

/**
 * The crack at rest, behind Raven. A dark tear: a sliver of nothing, darker than the wall,
 * with a lit rim — the light lives on the edge, the hole is the other side.
 *
 * Layers, cheapest first: a faint glow leaking round the tear; hairline fractures off it,
 * in the same hand as the shield's; the tear itself, black with a thin bright edge; one
 * slow pulse along the rim; and, rarely, a tendril that snaps off and dies. Motes fall in
 * the whole time and go dark as they cross the edge. Everything is strokes, fills and a few
 * gradients — no shadowBlur, no filters — so a frame is a few dozen draw calls.
 */
export default function RiftCanvas({ stage, figure, onGeometry }: Props) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let w = 0
    let h = 0
    let g: RiftGeometry | null = null
    let seam: number[] = []
    let frameBox: DOMRect | null = null
    const shards: Shard[] = []

    /** Nearest point on the spine to (x, y). */
    const nearestOnSpine = (x: number, y: number): [number, number] => {
      const gg = g as RiftGeometry
      const dx = Math.sin(gg.tilt)
      const dy = Math.cos(gg.tilt)
      const s = Math.max(-0.9, Math.min(0.9, ((x - gg.x) * dx + (y - gg.y) * dy) / (gg.len / 2)))
      return [gg.x + dx * s * (gg.len / 2), gg.y + dy * s * (gg.len / 2)]
    }
    /** The pieces that came off the letters. Sources are the chips, read from the DOM. */
    const spawnShards = () => {
      shards.length = 0
      if (!g) return
      const letters = stage.current?.querySelectorAll<HTMLElement>('.wordmark__letter[data-chip]') ?? []
      for (const el of letters) {
        const [fx, fy] = (el.dataset.chip ?? '0.5,0.5').split(',').map(Number)
        const b = el.getBoundingClientRect()
        const sx = b.left + b.width * (fx as number)
        const sy = b.top + b.height * (fy as number)
        const ink = getComputedStyle(el).getPropertyValue('--ink').trim() || '#d8b4fe'
        const [tx, ty] = nearestOnSpine(sx, sy)
        for (let i = 0; i < SHARDS_PER_CHIP; i++) {
          shards.push({
            sx, sy, tx, ty, ink,
            // Spread across the whole cycle so some are mid-flight on the first frame.
            t: Math.random() * 2.2 - 1.2,
            speed: 1 / (7000 + Math.random() * 6000),
            size: 3 + Math.random() * 4,
            rot: Math.random() * Math.PI * 2,
            spin: (Math.random() - 0.5) * 0.004,
            wobble: (Math.random() - 0.5) * 60,
          })
        }
      }
    }

    const measure = () => {
      const s = stage.current?.getBoundingClientRect()
      if (!s || s.width === 0) return
      // Layout height, not the transformed box: the evasion loop and hover scale move her a
      // little, and the crack must stay put — she moves around it, not it around her.
      const f = figure.current
      const rect = f ? layoutBoxIn(f, stage.current as HTMLElement, s) : null
      g = riftFromLayout(s, rect)
      // The shield is drawn on the stage's parent, the HUD frame.
      frameBox = stage.current?.parentElement?.getBoundingClientRect() ?? null
      seam = riftPolygon(g, 0, 0)
      onGeometry(g)
      spawnShards()
    }

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = window.innerWidth
      h = window.innerHeight
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      measure()
    }
    resize()
    window.addEventListener('resize', resize)
    // The hero art decides the crack's length, and it may not have decoded yet on mount.
    const img = figure.current
    img?.addEventListener('load', measure)

    const motes: Mote[] = Array.from({ length: MOTE_COUNT }, () => ({
      angle: Math.random() * Math.PI * 2,
      dist: 0.15 + Math.random() * 0.55,
      speed: 0.0008 + Math.random() * 0.0016,
      size: 0.8 + Math.random() * 1.4,
    }))
    const tendrils: Tendril[] = []

    /** A point on the spine at s ∈ [-1, 1]. */
    const spineAt = (s: number): [number, number] => {
      const gg = g as RiftGeometry
      return [gg.x + Math.sin(gg.tilt) * s * (gg.len / 2), gg.y + Math.cos(gg.tilt) * s * (gg.len / 2)]
    }

    let frame = 0
    let last = 0
    const render = (now: number) => {
      if (scene.covered) {
        last = now
        frame = requestAnimationFrame(render)
        return
      }
      const dt = last ? Math.min(50, now - last) : 16
      last = now
      ctx.clearRect(0, 0, w, h)
      if (g) {
        const breath = 0.5 + 0.5 * Math.sin(now / 1900)
        // A slight unevenness on the slow breath, so the rim is alive without twitching.
        const flicker = 0.92 + 0.08 * Math.abs(Math.sin(now / 90) * Math.sin(now / 210))
        ctx.globalCompositeOperation = 'lighter'
        ctx.lineCap = 'round'
        ctx.lineJoin = 'round'

        // ── Light bleeding through the wall ───────────────────────────────────────────
        const bleedR = g.len * 0.36
        const bleed = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, bleedR)
        bleed.addColorStop(0, `rgba(192, 132, 252, ${(0.07 + breath * 0.03).toFixed(3)})`)
        bleed.addColorStop(0.35, `rgba(147, 51, 234, ${(0.025 + breath * 0.012).toFixed(3)})`)
        bleed.addColorStop(1, 'rgba(92, 26, 160, 0)')
        ctx.fillStyle = bleed
        ctx.fillRect(g.x - bleedR, g.y - bleedR, bleedR * 2, bleedR * 2)

        // ── Threads: the shield is hers. Energy from each orb out to the rim ─────────
        // Read her live box: the float and the evasion drift move her every frame, and the
        // threads must leave from the orbs, not from where the orbs were on mount.
        const fig = figure.current?.getBoundingClientRect()
        if (fig && frameBox && fig.width > 0) {
          const fb = frameBox
          const orbs: Array<[number, number, 1 | -1]> = [
            [fig.left + fig.width * ORB.x, fig.top + fig.height * ORB.y, 1],
            [fig.right - fig.width * ORB.x, fig.top + fig.height * ORB.y, -1],
          ]
          let ti = 0
          for (const [ox, oy, side] of orbs) {
            for (const [ax, ay] of ANCHORS) {
              const tx = side === 1 ? fb.left + fb.width * ax : fb.right - fb.width * ax
              const ty = fb.top + fb.height * ay
              // Bow the thread a little and let it sway: a taut straight line reads as a wire.
              const mx = (ox + tx) / 2
              const my = (oy + ty) / 2
              const dx = tx - ox
              const dy = ty - oy
              const L = Math.hypot(dx, dy) || 1
              const sway = Math.sin(now / 900 + ti * 1.7) * 14 + Math.sin(now / 230 + ti) * 3
              const cx = mx + (-dy / L) * sway
              const cy = my + (dx / L) * sway
              const pulse = 0.5 + 0.5 * Math.sin(now / 640 + ti * 2.1)
              ctx.beginPath()
              ctx.moveTo(ox, oy)
              ctx.quadraticCurveTo(cx, cy, tx, ty)
              ctx.lineWidth = 4
              ctx.strokeStyle = `rgba(168, 85, 247, ${(0.05 + pulse * 0.03).toFixed(3)})`
              ctx.stroke()
              ctx.lineWidth = 1
              ctx.strokeStyle = `rgba(216, 180, 254, ${(0.14 + pulse * 0.1 * flicker).toFixed(3)})`
              ctx.stroke()
              // Where it meets the rim, the barrier takes the energy in.
              const ar = 14 + pulse * 6
              const anchor = ctx.createRadialGradient(tx, ty, 0, tx, ty, ar)
              anchor.addColorStop(0, `rgba(233, 213, 255, ${(0.35 + pulse * 0.2).toFixed(3)})`)
              anchor.addColorStop(1, 'rgba(168, 85, 247, 0)')
              ctx.fillStyle = anchor
              ctx.fillRect(tx - ar, ty - ar, ar * 2, ar * 2)
              ti++
            }
          }
        }

        // ── Fractures: hairlines, the same hand as the shield's cracks ───────────────
        for (const b of BRANCHES) {
          tracePath(ctx, branchPoints(g, b), false)
          ctx.lineWidth = 2.5
          ctx.strokeStyle = `rgba(168, 85, 247, ${(0.05 + breath * 0.02).toFixed(3)})`
          ctx.stroke()
          ctx.lineWidth = 0.9
          ctx.strokeStyle = `rgba(216, 180, 254, ${(0.22 + breath * 0.08).toFixed(3)})`
          ctx.stroke()
        }

        // ── The tear: a soft glow round it, then black, then a thin bright edge ──────
        tracePath(ctx, seam, true)
        ctx.lineWidth = 22
        ctx.strokeStyle = `rgba(168, 85, 247, ${(0.06 + breath * 0.04).toFixed(3)})`
        ctx.stroke()
        ctx.lineWidth = 7
        ctx.strokeStyle = `rgba(192, 132, 252, ${(0.1 * flicker).toFixed(3)})`
        ctx.stroke()
        // Darker than the wall: this is a hole, and what is through it is not lit.
        ctx.globalCompositeOperation = 'source-over'
        ctx.fillStyle = 'rgba(1, 0, 3, 0.96)'
        ctx.fill()
        ctx.globalCompositeOperation = 'lighter'
        ctx.lineWidth = 1.3
        ctx.strokeStyle = `rgba(233, 213, 255, ${(0.7 * flicker).toFixed(3)})`
        ctx.stroke()

        // ── One slow pulse along the rim ─────────────────────────────────────────────
        for (let i = 0; i < SURGE_COUNT; i++) {
          const phase = ((now + i * (SURGE_PERIOD_MS / SURGE_COUNT)) % SURGE_PERIOD_MS) / SURGE_PERIOD_MS
          const s = -1 + 2 * phase
          const [sx, sy] = spineAt(s)
          const ends = Math.min(1, (1 - Math.abs(s)) * 4) // fade in/out at the tips
          const r = 26 + breath * 6
          const surge = ctx.createRadialGradient(sx, sy, 0, sx, sy, r)
          surge.addColorStop(0, `rgba(233, 213, 255, ${(0.22 * ends).toFixed(3)})`)
          surge.addColorStop(0.35, `rgba(192, 132, 252, ${(0.1 * ends).toFixed(3)})`)
          surge.addColorStop(1, 'rgba(168, 85, 247, 0)')
          ctx.fillStyle = surge
          ctx.fillRect(sx - r, sy - r, r * 2, r * 2)
        }

        // ── Tendrils: bolts that snap off the seam and are gone in a few frames ──────
        if (Math.random() < (TENDRIL_RATE * dt) / 1000) {
          tendrils.push({ branch: randomTendril(), born: now, life: 90 + Math.random() * 160 })
        }
        for (let i = tendrils.length - 1; i >= 0; i--) {
          const t = tendrils[i] as Tendril
          const age = (now - t.born) / t.life
          if (age >= 1) {
            tendrils.splice(i, 1)
            continue
          }
          const a = 1 - age * age
          tracePath(ctx, branchPoints(g, t.branch), false)
          ctx.lineWidth = 4
          ctx.strokeStyle = `rgba(192, 132, 252, ${(0.12 * a).toFixed(3)})`
          ctx.stroke()
          ctx.lineWidth = 1
          ctx.strokeStyle = `rgba(240, 230, 255, ${(0.5 * a).toFixed(3)})`
          ctx.stroke()
        }

        // ── Motes drawn into the crack: the pull is there even while it is shut ──────
        for (const m of motes) {
          m.dist -= m.speed * (1.3 - m.dist)
          if (m.dist <= 0.01) {
            m.angle = Math.random() * Math.PI * 2
            m.dist = 0.4 + Math.random() * 0.4
          }
          const d = m.dist * g.len
          const x = g.x + Math.cos(m.angle) * d
          const y = g.y + Math.sin(m.angle) * d * 1.3
          // Brighten on the way in, then go out as they cross the edge into the dark.
          const a = Math.min(1, (0.6 - m.dist) * 2.4) * Math.min(1, (m.dist - 0.01) / 0.07)
          if (a <= 0) continue
          ctx.fillStyle = `rgba(233, 213, 255, ${(a * 0.55).toFixed(3)})`
          ctx.beginPath()
          ctx.arc(x, y, m.size, 0, Math.PI * 2)
          ctx.fill()
        }

        // ── Fragments: what the chips took off the letters, drifting to the seam ─────
        ctx.globalCompositeOperation = 'source-over'
        for (const sh of shards) {
          sh.t += sh.speed * dt
          if (sh.t >= 1) sh.t = -Math.random() * 1.5
          if (sh.t < 0) continue
          const u = sh.t
          const e = u * u // gentle at the letter, faster near the seam: the pull
          const dx = sh.tx - sh.sx
          const dy = sh.ty - sh.sy
          const L = Math.hypot(dx, dy) || 1
          const bow = Math.sin(u * Math.PI) * sh.wobble
          const x = sh.sx + dx * e + (-dy / L) * bow
          const y = sh.sy + dy * e + (dx / L) * bow
          const a = Math.min(1, u * 8) * Math.min(1, (1 - u) * 3) * 0.85
          const size = sh.size * (1 - 0.6 * e)
          sh.rot += sh.spin * dt
          ctx.save()
          ctx.translate(x, y)
          ctx.rotate(sh.rot)
          ctx.globalAlpha = a
          ctx.fillStyle = sh.ink
          ctx.beginPath()
          ctx.moveTo(-size, size * 0.6)
          ctx.lineTo(size * 0.9, size * 0.3)
          ctx.lineTo(-size * 0.2, -size)
          ctx.closePath()
          ctx.fill()
          ctx.restore()
        }
        ctx.globalCompositeOperation = 'source-over'
      }
      frame = requestAnimationFrame(render)
    }
    frame = requestAnimationFrame(render)

    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', resize)
      img?.removeEventListener('load', measure)
    }
  }, [stage, figure, onGeometry])

  return <canvas className="rift" ref={ref} aria-hidden="true" />
}
