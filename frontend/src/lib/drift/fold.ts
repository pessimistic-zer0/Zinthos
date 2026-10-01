/**
 * Drift being pulled into the rift — and torn back out of it.
 *
 * One clock drives both halves of it, on purpose (the same reason RiftReveal owns the warp):
 *
 *   the screen   the Drift root's transform and opacity, written each frame
 *   the light    a full-screen canvas above it: the band's lit rims, debris streaking at the
 *                crack, the dark closing in around it, and the flash and shockwave on landing
 *
 * Started a frame apart, the rims would slide off the band's edges while it moves at
 * thousands of px/s; computed from the same `p` they cannot.
 *
 * The fold, as `p` runs 0 → 1:
 *
 *   0    – .14  RECOIL   the screen swells and leans away from the crack — it resists
 *   .14  – .50  CRUSH    it is pressed to a band along the crack's lean, starting to move
 *   .50  – .66  TENSION  a taut, trembling line, stretched past the screen's width
 *   .66  – 1    SNAP     it is yanked into the crack, shrinking to the seam
 *
 * The way out runs the same function with p going 1 → 0: the snap becomes a burst out of the
 * crack, the recoil an overshoot that settles. Impact effects (flash, ring, shake) happen at
 * the crack on the way in, and at the start of the way out.
 *
 * Canvas rules as elsewhere in the repo: no shadowBlur — glow is a wide low-alpha stroke under
 * a thin bright one.
 */
import { fx } from '../player'

export interface FoldTarget {
  /** The crack's centre, viewport px. */
  x: number
  y: number
  /** Its untransformed width, px. */
  w: number
  /** Its lean, radians. */
  rot: number
}

interface Pose {
  dx: number
  dy: number
  sx: number
  sy: number
  rot: number
  opacity: number
}

const IN_MS = 1150
const OUT_MS = 820
/** Dev only: slow the fold down to look at it (window.__foldSlow = 10). */
const slow = (): number => (import.meta.env.DEV ? Number((window as { __foldSlow?: number }).__foldSlow) || 1 : 1)

const clamp01 = (v: number): number => Math.max(0, Math.min(1, v))
const lerp = (a: number, b: number, t: number): number => a + (b - a) * t
const easeOut = (t: number): number => 1 - Math.pow(1 - t, 3)
const easeInOut = (t: number): number => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)
const easeIn = (t: number): number => t * t * t * t
const seg = (p: number, a: number, b: number): number => clamp01((p - a) / (b - a))

/** Where the screen is at `p` — the four phases above. `d` is the offset to the crack. */
function pose(p: number, t: FoldTarget, W: number, H: number, clock: number): Pose {
  const dx = t.x - W / 2
  const dy = t.y - H / 2
  const sxEnd = Math.max(0.02, t.w / W)
  if (p < 0.14) {
    const e = easeOut(p / 0.14)
    return { dx: -0.04 * dx * e, dy: -0.04 * dy * e, sx: 1 + 0.045 * e, sy: 1 + 0.045 * e, rot: -0.35 * t.rot * e, opacity: 1 }
  }
  if (p < 0.5) {
    const e = easeInOut(seg(p, 0.14, 0.5))
    return {
      dx: lerp(-0.04 * dx, 0.16 * dx, e),
      dy: lerp(-0.04 * dy, 0.16 * dy, e),
      sx: lerp(1.045, 0.96, e),
      sy: lerp(1.045, 0.035, e),
      rot: lerp(-0.35 * t.rot, t.rot, e),
      opacity: 1,
    }
  }
  if (p < 0.66) {
    const e = easeOut(seg(p, 0.5, 0.66))
    // Trembling: a few px of shiver across the line, the only unsmooth motion in the fold.
    const shiver = Math.sin(clock * 0.09) * 2.2 + Math.sin(clock * 0.23) * 1.3
    return {
      dx: lerp(0.16 * dx, 0.1 * dx, e) - Math.sin(t.rot) * shiver,
      dy: lerp(0.16 * dy, 0.1 * dy, e) + Math.cos(t.rot) * shiver,
      sx: lerp(0.96, 1.14, e),
      sy: lerp(0.035, 0.012, e),
      rot: t.rot,
      opacity: 1,
    }
  }
  const e = easeIn(seg(p, 0.66, 1))
  return {
    dx: lerp(0.1 * dx, dx, e),
    dy: lerp(0.1 * dy, dy, e),
    sx: lerp(1.14, sxEnd, e),
    sy: lerp(0.012, 0.003, e),
    rot: t.rot,
    opacity: 1 - seg(p, 0.9, 1),
  }
}

// ── The light ────────────────────────────────────────────────────────────────────────

interface Mote {
  x: number
  y: number
  /** 0-1 along its path, and how fast it moves along it. */
  k: number
  speed: number
  /** When it sets off, as p (way in) or 1 − p (way out). */
  at: number
  width: number
  bright: boolean
}

function rng(seed: number): () => number {
  let s = seed
  return () => {
    s = (s * 16807) % 2147483647
    return s / 2147483647
  }
}

/** Points along a rim: jagged, fixed per fold, so the edge crackles without crawling. */
function jag(n: number, seed: number): Float32Array {
  const r = rng(seed)
  const out = new Float32Array(n)
  for (let i = 0; i < n; i++) out[i] = r() * 2 - 1
  return out
}

function makeCanvas(): [HTMLCanvasElement, CanvasRenderingContext2D, number] {
  const c = document.createElement('canvas')
  c.className = 'drift-fold-fx'
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5)
  c.width = Math.round(window.innerWidth * dpr)
  c.height = Math.round(window.innerHeight * dpr)
  document.body.append(c)
  const g = c.getContext('2d')!
  g.setTransform(dpr, 0, 0, dpr, 0, 0)
  return [c, g, dpr]
}

const RIM = 'rgba(216, 180, 254,'
const GLOW = 'rgba(168, 85, 247,'

function drawRims(g: CanvasRenderingContext2D, cx: number, cy: number, len: number, thick: number, rot: number, strength: number, jagA: Float32Array, jagB: Float32Array): void {
  if (strength <= 0.01) return
  const n = jagA.length
  const ux = Math.cos(rot)
  const uy = Math.sin(rot)
  const nx = -uy
  const ny = ux
  for (const [side, j] of [
    [-1, jagA],
    [1, jagB],
  ] as const) {
    g.beginPath()
    for (let i = 0; i < n; i++) {
      const t = i / (n - 1) - 0.5
      // Pinched at the ends, like the crack itself: a lens, not a bar.
      const pinch = Math.sqrt(Math.max(0, 1 - 4 * t * t))
      const off = side * (thick / 2) * pinch + (j[i] ?? 0) * Math.min(6, 1 + thick * 0.06) * pinch
      const x = cx + ux * t * len + nx * off
      const y = cy + uy * t * len + ny * off
      if (i === 0) g.moveTo(x, y)
      else g.lineTo(x, y)
    }
    g.lineJoin = 'round'
    g.strokeStyle = `${GLOW}${0.16 * strength})`
    g.lineWidth = 16
    g.stroke()
    g.strokeStyle = `${RIM}${0.5 * strength})`
    g.lineWidth = 4
    g.stroke()
    g.strokeStyle = `rgba(255, 255, 255, ${0.95 * strength})`
    g.lineWidth = 1.2
    g.stroke()
  }
}

function drawImpact(g: CanvasRenderingContext2D, x: number, y: number, age: number): void {
  // age 0 → 1 over the aftermath.
  const a = 1 - age
  if (a <= 0) return
  const flash = g.createRadialGradient(x, y, 0, x, y, 180 + age * 120)
  flash.addColorStop(0, `rgba(255, 255, 255, ${0.9 * a * a})`)
  flash.addColorStop(0.18, `${RIM}${0.55 * a * a})`)
  flash.addColorStop(1, `${GLOW}0)`)
  g.fillStyle = flash
  g.fillRect(x - 320, y - 320, 640, 640)
  for (const [delay, reach] of [
    [0, 340],
    [0.12, 220],
  ] as const) {
    const t = clamp01((age - delay) / (1 - delay))
    if (t <= 0 || t >= 1) continue
    const r = easeOut(t) * reach
    g.beginPath()
    g.ellipse(x, y, r, r * 0.62, 0, 0, Math.PI * 2)
    g.strokeStyle = `${GLOW}${0.22 * (1 - t)})`
    g.lineWidth = 14 * (1 - t) + 2
    g.stroke()
    g.strokeStyle = `rgba(255, 255, 255, ${0.8 * (1 - t)})`
    g.lineWidth = 1.4
    g.stroke()
  }
}

// ── The run ──────────────────────────────────────────────────────────────────────────

export interface FoldRun {
  done: Promise<void>
  cancel(): void
}

/**
 * Fold `root` into `target` (`dir` 'in'), or tear it back out ('out'). Resolves when the
 * screen part is finished; the aftermath (ring, flash) plays on for a moment after.
 */
export function runFold(root: HTMLElement, target: FoldTarget, dir: 'in' | 'out', onImpact?: () => void): FoldRun {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const W = window.innerWidth
  const H = window.innerHeight
  const dur = (dir === 'in' ? IN_MS : OUT_MS) * slow()
  const after = 520 * slow()

  // The initial fade-in's fill would otherwise hold opacity at 1 over our inline style.
  root.style.animation = 'none'

  if (reduced) {
    const end = dir === 'in' ? pose(1, target, W, H, 0) : null
    apply(root, end)
    if (dir === 'in') onImpact?.()
    return { done: Promise.resolve(), cancel: () => {} }
  }

  const [canvas, g] = makeCanvas()
  const seed = Math.floor(Math.random() * 1e6) + 1
  const jagA = jag(64, seed)
  const jagB = jag(64, seed + 7)
  const r = rng(seed + 13)
  // Debris from all over the screen, weighted to the edges: that is where suction reads.
  const motes: Mote[] = Array.from({ length: 170 }, () => {
    const edge = r() < 0.65
    const x = edge ? (r() < 0.5 ? r() * W * 0.18 : W - r() * W * 0.18) : r() * W
    const y = edge ? r() * H : r() < 0.5 ? r() * H * 0.2 : H - r() * H * 0.2
    return { x, y, k: 0, speed: 0.8 + r() * 1.4, at: 0.12 + r() * 0.62, width: 0.6 + r() * 1.6, bright: r() < 0.25 }
  })

  if (dir === 'in') {
    fx.whoosh(dur / 1000, true)
    fx.muffle(420, (dur / 1000) * 0.95)
  } else {
    fx.whoosh((dur / 1000) * 0.9, false)
    fx.muffle(420, 0.02)
    fx.muffle(20000, (dur / 1000) * 1.1)
    onImpact?.()
  }

  let raf = 0
  let cancelled = false
  let resolve: () => void = () => {}
  const done = new Promise<void>((res) => (resolve = res))
  const t0 = performance.now()
  let impacted = dir === 'out'
  let impactAt = dir === 'out' ? t0 : 0

  const frame = (now: number) => {
    if (cancelled) return
    const el = now - t0
    const u = clamp01(el / dur)
    const p = dir === 'in' ? u : 1 - u
    const P = pose(p, target, W, H, el)
    if (u < 1) apply(root, P)

    // Where the band is, in screen space.
    const cx = W / 2 + P.dx
    const cy = H / 2 + P.dy
    const len = W * P.sx
    const thick = H * P.sy

    g.clearRect(0, 0, W, H)

    // The dark closing in: strongest while the band is thin and moving.
    const dark = 0.55 * Math.sin(Math.PI * clamp01((p - 0.1) / 0.9)) * (u < 1 ? 1 : 0)
    if (dark > 0.01) {
      g.fillStyle = `rgba(4, 2, 9, ${dark})`
      g.fillRect(0, 0, W, H)
    }

    // Debris: in, it streaks from the edges at the band (and, late, the crack); out, from the
    // crack to the edges.
    if (u < 1) {
      for (const m of motes) {
        const live = dir === 'in' ? p - m.at : 1 - p - m.at * 0.6
        if (live <= 0) continue
        m.k = clamp01(easeIn(clamp01(live * m.speed * 1.8)) * 1.02)
        const aimX = lerp(cx, target.x, clamp01((p - 0.6) / 0.4))
        const aimY = lerp(cy, target.y, clamp01((p - 0.6) / 0.4))
        const [fx0, fy0, tx, ty] = dir === 'in' ? [m.x, m.y, aimX, aimY] : [target.x, target.y, m.x, m.y]
        const k = dir === 'in' ? m.k : easeOut(clamp01(live * m.speed * 1.4))
        const x = lerp(fx0, tx, k)
        const y = lerp(fy0, ty, k)
        // A streak behind it, longer the faster it goes.
        const tail = dir === 'in' ? 0.05 + k * 0.18 : 0.12 * (1 - k)
        const bx = lerp(fx0, tx, Math.max(0, k - tail))
        const by = lerp(fy0, ty, Math.max(0, k - tail))
        const alpha = (dir === 'in' ? Math.min(1, live * 6) * (1 - Math.pow(k, 8)) : 1 - k) * (m.bright ? 0.95 : 0.55)
        if (alpha <= 0.01) continue
        g.beginPath()
        g.moveTo(bx, by)
        g.lineTo(x, y)
        g.strokeStyle = m.bright ? `rgba(255, 255, 255, ${alpha})` : `${RIM}${alpha})`
        g.lineWidth = m.width
        g.stroke()
      }
    }

    // The band's rims: they light as it thins — the crack closing over it.
    if (u < 1) {
      const rim = clamp01((0.9 - P.sy) / 0.5) * (1 - seg(p, 0.93, 1))
      drawRims(g, cx, cy, len, Math.max(1, thick), P.rot, rim, jagA, jagB)
    }

    // Landing.
    if (!impacted && dir === 'in' && u >= 1) {
      impacted = true
      impactAt = now
      apply(root, pose(1, target, W, H, el))
      onImpact?.()
      resolve()
    }
    if (impacted) {
      const age = (now - impactAt) / after
      drawImpact(g, target.x, target.y, clamp01(age))
      if (dir === 'out' && u >= 1) {
        apply(root, null)
        resolve()
      }
      if (age >= 1 && u >= 1) {
        canvas.remove()
        return
      }
    }
    raf = requestAnimationFrame(frame)
  }
  raf = requestAnimationFrame(frame)

  return {
    done,
    cancel() {
      cancelled = true
      cancelAnimationFrame(raf)
      canvas.remove()
      resolve()
    },
  }
}

function apply(root: HTMLElement, P: Pose | null): void {
  if (!P) {
    root.style.transform = ''
    root.style.opacity = ''
    return
  }
  root.style.transform = `translate(${P.dx.toFixed(1)}px, ${P.dy.toFixed(1)}px) rotate(${P.rot.toFixed(4)}rad) scale(${P.sx.toFixed(4)}, ${P.sy.toFixed(4)})`
  root.style.opacity = P.opacity.toFixed(3)
}

/**
 * The crack taking the screen in: it tears wide and bounces shut, the console jolts, Raven's
 * photo pushes in on the hit. Transform (and the separate `translate`/`scale` properties, so
 * nothing already on those elements is clobbered) and opacity only.
 */
export function impact(seam: HTMLElement | null): void {
  fx.thud()
  fx.muffle(20000, 0.9)
  if (seam) {
    const base = getComputedStyle(seam).transform
    const t = base && base !== 'none' ? base : ''
    seam.animate(
      [
        { transform: `${t} scaleY(1)` },
        { transform: `${t} scaleY(5.5)`, offset: 0.12 },
        { transform: `${t} scaleY(0.6)`, offset: 0.4 },
        { transform: `${t} scaleY(1.6)`, offset: 0.62 },
        { transform: `${t} scaleY(1)` },
      ],
      { duration: 900 * slow(), easing: 'ease-out' },
    )
  }
  const box = seam?.closest<HTMLElement>('.console') ?? seam?.closest<HTMLElement>('.np')
  box?.animate(
    [
      { translate: '0 0' },
      { translate: '-9px 5px', offset: 0.1 },
      { translate: '7px -4px', offset: 0.25 },
      { translate: '-4px 3px', offset: 0.45 },
      { translate: '2px -1px', offset: 0.7 },
      { translate: '0 0' },
    ],
    { duration: 480 * slow(), easing: 'ease-out' },
  )
  const photo = seam?.parentElement?.querySelector<HTMLElement>('img')
  photo?.animate([{ scale: '1' }, { scale: '1.09', offset: 0.15 }, { scale: '1' }], {
    duration: 700 * slow(),
    easing: 'cubic-bezier(0.2, 0.9, 0.3, 1)',
  })
}
