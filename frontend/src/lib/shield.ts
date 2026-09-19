/**
 * The shield: the barrier Raven holds between the visitor and the rift.
 *
 * A rounded pane the size of the HUD frame, with its rim bitten into — chips of varying
 * width and depth, one corner taken clean off — hairline fractures running in from the
 * deeper bites, stretches where the rim has worn away, and a few shards floating just
 * outside. Everything is seeded so the damage is the same on every visit: this is a shield
 * that has been holding for a long time, not one that shatters differently per reload.
 *
 * Output is SVG path strings. The component draws them once; nothing here animates.
 */
import { mulberry32 } from './rift'

export interface ShieldPaths {
  /** Closed outline of the pane, chips included. Fill only. */
  pane: string
  /** Intact stretches of the rim. */
  rim: string
  /** Freshly broken edges — the inside of every chip. Drawn brighter. */
  raw: string
  /** Hairline cracks running into the pane from the deeper chips. */
  cracks: string
  /** Broken-off fragments just outside the rim. */
  shards: string
  /** The hardest hits: centre just inside the bite and a radius the energy rippled to. */
  impacts: Array<{ x: number; y: number; r: number }>
}

const SEED = 0x5a1e
/** Corner radius of the undamaged pane. */
const RADIUS = 18
/** Sampling step along the rim, px. Fine enough that corners stay round. */
const STEP = 5
const CHIP_COUNT = 15
const WEAR_COUNT = 5
/** Fractures that start on the intact rim rather than inside a bite. */
const RIM_CRACK_COUNT = 4
/** Of those, how many run long — stress cracks from blows the shield barely took. */
const LONG_CRACK_COUNT = 2

interface Chip {
  /** Distance along the perimeter where the bite starts. */
  start: number
  width: number
  depth: number
  /** Interior vertices as (along-offset, inward-depth) pairs. */
  verts: Array<[number, number]>
}

interface Pt {
  x: number
  y: number
  /** Inward unit normal. */
  nx: number
  ny: number
}

/** Point and inward normal at distance `d` clockwise around a rounded rect. */
function walker(x0: number, y0: number, x1: number, y1: number, r: number) {
  const sw = x1 - x0 - 2 * r
  const sh = y1 - y0 - 2 * r
  const arc = (Math.PI * r) / 2
  const total = 2 * sw + 2 * sh + 4 * arc
  const corner = (cx: number, cy: number, a0: number, u: number): Pt => {
    const a = a0 + (u * Math.PI) / 2
    const c = Math.cos(a)
    const s = Math.sin(a)
    return { x: cx + r * c, y: cy + r * s, nx: -c, ny: -s }
  }
  const at = (dIn: number): Pt => {
    let d = ((dIn % total) + total) % total
    if (d < sw) return { x: x0 + r + d, y: y0, nx: 0, ny: 1 }
    d -= sw
    if (d < arc) return corner(x1 - r, y0 + r, -Math.PI / 2, d / arc)
    d -= arc
    if (d < sh) return { x: x1, y: y0 + r + d, nx: -1, ny: 0 }
    d -= sh
    if (d < arc) return corner(x1 - r, y1 - r, 0, d / arc)
    d -= arc
    if (d < sw) return { x: x1 - r - d, y: y1, nx: 0, ny: -1 }
    d -= sw
    if (d < arc) return corner(x0 + r, y1 - r, Math.PI / 2, d / arc)
    d -= arc
    if (d < sh) return { x: x0, y: y1 - r - d, nx: 1, ny: 0 }
    d -= sh
    return corner(x0 + r, y0 + r, Math.PI, d / arc)
  }
  return { at, total, topRightCorner: sw + arc / 2 }
}

const f1 = (n: number): string => n.toFixed(1)

/**
 * @param w,h  size of the frame box the shield covers
 * @param m    margin around it that the SVG also covers, so shards can sit outside the rim
 */
export function shieldPaths(w: number, h: number, m: number): ShieldPaths {
  const rnd = mulberry32(SEED)
  const inset = 1.5
  const { at, total, topRightCorner } = walker(m + inset, m + inset, m + w - inset, m + h - inset, RADIUS)

  // ── Chips: bites out of the rim, spaced so none overlap ─────────────────────────────
  const chips: Chip[] = []
  const place = (start: number, width: number, depth: number) => {
    const verts: Array<[number, number]> = []
    const n = 3 + Math.floor(rnd() * 4)
    for (let j = 1; j <= n; j++) {
      const u = j / (n + 1)
      // Deepest toward the middle, jagged everywhere.
      const lens = Math.sqrt(Math.max(0, 1 - Math.pow(2 * u - 1, 2)))
      verts.push([width * (u + ((rnd() - 0.5) * 0.5) / (n + 1)), depth * lens * (0.5 + rnd() * 0.5)])
    }
    chips.push({ start, width, depth, verts })
  }
  // One corner taken clean off: the blow that nearly got through.
  place(topRightCorner - 48, 96, 40)
  for (let tries = 0; chips.length < CHIP_COUNT && tries < 160; tries++) {
    // Two in five are nicks — small, shallow, the kind a shield collects over years.
    const nick = rnd() < 0.4
    const width = nick ? 10 + rnd() * 22 : 26 + rnd() * 84
    const depth = nick ? 4 + rnd() * 8 : 9 + rnd() * 30
    const start = rnd() * (total - width)
    const clear = chips.every((c) => start + width + 26 < c.start || start > c.start + c.width + 26)
    if (clear) place(start, width, depth)
  }
  chips.sort((a, b) => a.start - b.start)

  // ── Wear: stretches where the rim's light has gone out ──────────────────────────────
  const wear: Array<[number, number]> = []
  for (let i = 0; i < WEAR_COUNT; i++) {
    const len = 36 + rnd() * 120
    const s = rnd() * (total - len)
    wear.push([s, s + len])
  }
  const worn = (d: number) => wear.some(([a, b]) => d >= a && d <= b)

  // ── Walk the rim, biting where a chip sits ──────────────────────────────────────────
  type Kind = 'rim' | 'raw' | 'worn'
  const pts: Array<{ x: number; y: number; kind: Kind }> = []
  let ci = 0
  for (let d = 0; d < total; ) {
    const c = chips[ci]
    if (c && d >= c.start) {
      const a = at(c.start)
      pts.push({ x: a.x, y: a.y, kind: 'raw' })
      for (const [along, depth] of c.verts) {
        const p = at(c.start + along)
        pts.push({ x: p.x + p.nx * depth, y: p.y + p.ny * depth, kind: 'raw' })
      }
      const b = at(c.start + c.width)
      pts.push({ x: b.x, y: b.y, kind: 'raw' })
      d = c.start + c.width + STEP
      ci++
      continue
    }
    const p = at(d)
    pts.push({ x: p.x, y: p.y, kind: worn(d) ? 'worn' : 'rim' })
    d += STEP
  }

  // ── Paths ───────────────────────────────────────────────────────────────────────────
  const pane = `M${pts.map((p) => `${f1(p.x)} ${f1(p.y)}`).join('L')}Z`

  // A segment is raw if either end is raw; worn if either end is worn; else rim.
  const runs: Record<Kind, string[]> = { rim: [], raw: [], worn: [] }
  let open: Kind | null = null
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]!
    const b = pts[(i + 1) % pts.length]!
    const kind: Kind =
      a.kind === 'raw' || b.kind === 'raw' ? 'raw' : a.kind === 'worn' || b.kind === 'worn' ? 'worn' : 'rim'
    if (kind !== open) {
      runs[kind].push(`M${f1(a.x)} ${f1(a.y)}`)
      open = kind
    }
    runs[kind].push(`L${f1(b.x)} ${f1(b.y)}`)
  }

  // ── Cracks ──────────────────────────────────────────────────────────────────────────
  const cracks: string[] = []
  /**
   * Grow a fracture: a polyline that wanders off `heading`, and may throw a shorter branch
   * from one of its joints. Real cracks fork; a crack that never does reads as a scratch.
   */
  const grow = (x: number, y: number, heading: number, segs: number, lo: number, hi: number, drift: number, gen = 0) => {
    cracks.push(`M${f1(x)} ${f1(y)}`)
    const forkAt = gen < 2 && rnd() < 0.4 ? 1 + Math.floor(rnd() * Math.max(1, segs - 1)) : -1
    let fx = 0
    let fy = 0
    let fh = 0
    for (let s = 0; s < segs; s++) {
      heading += (rnd() - 0.5) * drift
      const len = lo + rnd() * (hi - lo)
      x += Math.cos(heading) * len
      y += Math.sin(heading) * len
      cracks.push(`L${f1(x)} ${f1(y)}`)
      if (s === forkAt) {
        fx = x
        fy = y
        fh = heading + (rnd() < 0.5 ? -1 : 1) * (0.5 + rnd() * 0.7)
      }
    }
    if (forkAt >= 0) grow(fx, fy, fh, 1 + Math.floor(rnd() * 3), lo * 0.6, hi * 0.6, drift, gen + 1)
  }
  const inChip = (d: number) => chips.some((c) => d >= c.start - 6 && d <= c.start + c.width + 6)

  // From inside the bites: every bite past a nick has cracked further.
  for (const c of chips) {
    if (c.depth < 16) continue
    const n = rnd() < 0.35 ? 2 : 1
    for (let k = 0; k < n; k++) {
      const [along, depth] = c.verts[Math.floor(rnd() * c.verts.length)]!
      const p = at(c.start + along)
      const heading = Math.atan2(p.ny, p.nx) + (rnd() - 0.5) * 1.3
      grow(p.x + p.nx * depth, p.y + p.ny * depth, heading, 2 + Math.floor(rnd() * 4), 10, 34, 0.9)
    }
  }
  // From the intact rim: hairlines where the edge held but the glass behind it did not.
  for (let i = 0, made = 0; made < RIM_CRACK_COUNT && i < 60; i++) {
    const d = rnd() * total
    if (inChip(d)) continue
    const p = at(d)
    const heading = Math.atan2(p.ny, p.nx) + (rnd() - 0.5) * 1.0
    const long = made < LONG_CRACK_COUNT
    if (long) grow(p.x, p.y, heading, 5 + Math.floor(rnd() * 3), 20, 42, 0.5)
    else grow(p.x, p.y, heading, 2 + Math.floor(rnd() * 3), 10, 30, 0.9)
    made++
  }

  // ── Shards: pieces that came off, hanging just outside ──────────────────────────────
  const shards: string[] = []
  for (const c of chips) {
    if (rnd() < 0.45) continue
    const n = 1 + Math.floor(rnd() * 2)
    for (let k = 0; k < n; k++) {
      const p = at(c.start + c.width * (0.2 + rnd() * 0.6))
      const out = 5 + rnd() * (m - 6)
      const side = (rnd() - 0.5) * 30
      const cx = p.x - p.nx * out - p.ny * side
      const cy = p.y - p.ny * out + p.nx * side
      const size = 2.5 + rnd() * 4.5
      const a0 = rnd() * Math.PI * 2
      const tri = [0, 1, 2].map((i) => {
        const a = a0 + (i * Math.PI * 2) / 3 + (rnd() - 0.5) * 0.8
        const rr = size * (0.6 + rnd() * 0.6)
        return `${f1(cx + Math.cos(a) * rr)} ${f1(cy + Math.sin(a) * rr)}`
      })
      shards.push(`M${tri.join('L')}Z`)
    }
  }

  // The three deepest bites are where the barrier took real blows.
  const impacts = [...chips]
    .sort((a, b) => b.depth - a.depth)
    .slice(0, 3)
    .map((c) => {
      const p = at(c.start + c.width / 2)
      return { x: p.x + p.nx * c.depth * 0.4, y: p.y + p.ny * c.depth * 0.4, r: 70 + c.depth * 3 }
    })

  return {
    pane,
    impacts,
    rim: runs.rim.join(''),
    raw: runs.raw.join(''),
    cracks: cracks.join(''),
    shards: shards.join(''),
  }
}
