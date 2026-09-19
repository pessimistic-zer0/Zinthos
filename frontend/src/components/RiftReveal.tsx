import { useEffect, useRef } from 'react'
import type { WarpOrigin } from '../App'
import { riftCloseMs, warpDurationMs } from '../lib/warp'
import {
  BRANCHES,
  branchPoints,
  openingAt,
  polygonToClip,
  reachOf,
  riftPolygon,
  tracePath,
} from '../lib/rift'

const DEBRIS_COUNT = 120
/** Raven's timeline: recoil until here… */
const RECOIL_UNTIL = 0.24
/** …then pulled in until here, and gone shortly after. */
const TAKEN_AT = 0.66
const GONE_AT = 0.74

interface Debris {
  angle: number
  /** Starting distance from the crack, in units of reach. */
  from: number
  /** Timeline moment it starts falling. */
  delay: number
  width: number
}

/**
 * The rift opening and swallowing the screen. The idle tear is already dark, so there is no
 * white to hand over: the hole simply grows, and only its rim is lit.
 *
 * The hole itself is `clip-path: polygon()` on the choose screen, rewritten every frame
 * from `--rift-clip` on the root element. This canvas paints what the clip cannot: the
 * flash as the wall gives, the lit edges of the tear, the branches dying out, and debris
 * streaking INWARD — the one cue that says sucked rather than revealed.
 *
 * It also draws Raven. The choose screen sits above the landing while the crack opens, so
 * her DOM copy would be cut by the hole; this canvas is above both, so here she can stand
 * in front of the tear, recoil, and be pulled through it — picked up from the exact box she
 * had at the click so nothing jumps.
 *
 * One clock drives clip, light and Raven. A CSS keyframe for the clip and a rAF loop for
 * the light start a frame apart, and at peak the edge moves thousands of px/s.
 *
 * `mode="close"` runs the whole thing backwards for the way out: the clock still counts
 * 0 to 1, but every stage reads `1 - t`, so the hole shrinks down the same curve it grew
 * along, the rim lights up as it tightens, the debris streaks back out and the seam's
 * hairlines return at the end. Both ends stay continuous by construction — the first
 * frame of the close is the last frame of the open, and the last is the idle seam the
 * landing draws for itself. Raven is the one exception: she is standing in the DOM behind
 * the hole the whole way out, so painting her here too would double her.
 */
export default function RiftReveal({
  origin,
  mode = 'open',
}: {
  origin: WarpOrigin
  mode?: 'open' | 'close'
}) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const closing = mode === 'close'
    const duration = closing ? riftCloseMs() : warpDurationMs()
    const root = document.documentElement
    let w = 0
    let h = 0
    let reach = 0

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = window.innerWidth
      h = window.innerHeight
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      reach = reachOf(origin, w, h)
    }
    resize()
    window.addEventListener('resize', resize)

    const debris: Debris[] = Array.from({ length: DEBRIS_COUNT }, () => ({
      angle: Math.random() * Math.PI * 2,
      from: 0.3 + Math.random() * 1.1,
      delay: 0.06 + Math.random() * 0.56,
      width: 0.8 + Math.random() * 1.4,
    }))

    // Same bitmap the landing showed, so it is already decoded and in cache.
    const hero = origin.figure ? new Image() : null
    if (hero && origin.figure) hero.src = origin.figure.src

    const easeOut = (u: number) => 1 - Math.pow(1 - u, 3)
    const easeIn = (u: number) => Math.pow(u, 2.6)

    /** Draw Raven at time t: recoil, then pulled into the seam, shrinking and spinning. */
    const drawRaven = (t: number) => {
      const f = origin.figure
      if (!f || !hero || !hero.complete || hero.naturalWidth === 0 || t >= GONE_AT) return
      const cx0 = f.x + f.w / 2
      const cy0 = f.y + f.h * 0.46 // pivot on her chest, where the orbs are
      let scale: number
      let rot: number
      let cx: number
      let cy: number
      let alpha = 1
      if (t < RECOIL_UNTIL) {
        const u = easeOut(t / RECOIL_UNTIL)
        scale = 1 + 0.05 * u
        rot = -4 * u
        cx = cx0
        cy = cy0 + 8 * u
      } else {
        const u = easeIn(Math.min(1, (t - RECOIL_UNTIL) / (TAKEN_AT - RECOIL_UNTIL)))
        scale = 1.05 + (0.04 - 1.05) * u
        rot = -4 + 44 * u
        cx = cx0 + (origin.x - cx0) * u
        cy = cy0 + 8 + (origin.y - cy0 - 8) * u
        if (t > TAKEN_AT) alpha = 1 - (t - TAKEN_AT) / (GONE_AT - TAKEN_AT)
      }
      ctx.save()
      ctx.globalCompositeOperation = 'source-over'
      ctx.globalAlpha = alpha
      ctx.translate(cx, cy)
      ctx.rotate((rot * Math.PI) / 180)
      ctx.scale(scale, scale)
      ctx.drawImage(hero, -f.w / 2, -f.h * 0.46, f.w, f.h)
      ctx.restore()
      ctx.globalCompositeOperation = 'lighter'
    }

    const start = performance.now()
    let frame = 0

    const render = (now: number) => {
      const t = Math.min((now - start) / duration, 1)
      // Where on the opening's timeline this frame sits. Closing walks it in reverse.
      const p = closing ? 1 - t : t
      const k = openingAt(p)
      const pts = riftPolygon(origin, k, reach)
      root.style.setProperty('--rift-clip', polygonToClip(pts))

      ctx.clearRect(0, 0, w, h)
      ctx.globalCompositeOperation = 'lighter'
      ctx.lineCap = 'round'
      ctx.lineJoin = 'round'
      const { x, y } = origin
      const strength = p < 0.9 ? 1 : 1 - (p - 0.9) / 0.1

      // ── The wall gives: a dim pulse from the seam as the tear starts to move ───────
      if (p < 0.35) {
        const f = Math.pow(1 - p / 0.35, 1.5)
        const fr = 40 + p * 700
        const flash = ctx.createRadialGradient(x, y, 0, x, y, fr)
        flash.addColorStop(0, `rgba(233, 213, 255, ${(0.3 * f).toFixed(3)})`)
        flash.addColorStop(0.3, `rgba(192, 132, 252, ${(0.12 * f).toFixed(3)})`)
        flash.addColorStop(1, 'rgba(168, 85, 247, 0)')
        ctx.fillStyle = flash
        ctx.fillRect(x - fr, y - fr, fr * 2, fr * 2)
      }

      // ── Branches: the hairlines die as the main tear takes everything ───────────────
      if (p < 0.3) {
        const a = 1 - p / 0.3
        for (const b of BRANCHES) {
          tracePath(ctx, branchPoints(origin, b), false)
          ctx.lineWidth = 0.9
          ctx.strokeStyle = `rgba(216, 180, 254, ${(0.3 * a).toFixed(3)})`
          ctx.stroke()
        }
      }

      // ── The lit edge of the tear ────────────────────────────────────────────────────
      tracePath(ctx, pts, true)
      ctx.lineWidth = 18
      ctx.strokeStyle = `rgba(168, 85, 247, ${(0.12 * strength).toFixed(3)})`
      ctx.stroke()
      ctx.lineWidth = 6
      ctx.strokeStyle = `rgba(192, 132, 252, ${(0.18 * strength).toFixed(3)})`
      ctx.stroke()
      ctx.lineWidth = 1.6
      ctx.strokeStyle = `rgba(245, 236, 255, ${(0.8 * strength).toFixed(3)})`
      ctx.stroke()

      // ── Raven, in front of the tear until it takes her ──────────────────────────────
      if (!closing) drawRaven(p)

      // ── Debris falling in ───────────────────────────────────────────────────────────
      // Each streak accelerates toward the crack and stretches as it goes; the direction
      // of travel is what sells the pull.
      for (const d of debris) {
        const q = (p - d.delay) / 0.4
        if (q <= 0 || q >= 1) continue
        const e = q * q * q
        const dist = d.from * reach * (1 - e)
        const cx = x + Math.cos(d.angle) * dist
        const cy = y + Math.sin(d.angle) * dist
        const len = 6 + 90 * q * q
        const tx = x + Math.cos(d.angle) * Math.max(0, dist - len)
        const ty = y + Math.sin(d.angle) * Math.max(0, dist - len)
        const a = 0.75 * (1 - e) * Math.min(1, q * 8)
        ctx.lineWidth = d.width
        ctx.strokeStyle = `rgba(233, 213, 255, ${a.toFixed(3)})`
        ctx.beginPath()
        ctx.moveTo(cx, cy)
        ctx.lineTo(tx, ty)
        ctx.stroke()
      }

      ctx.globalCompositeOperation = 'source-over'
      if (t < 1) frame = requestAnimationFrame(render)
    }

    frame = requestAnimationFrame(render)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', resize)
      root.style.removeProperty('--rift-clip')
    }
  }, [origin, mode])

  return <canvas className="rift-reveal" ref={ref} aria-hidden="true" />
}
