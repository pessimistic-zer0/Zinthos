/**
 * The rift: a crack in the dimension wall behind Raven.
 *
 * One geometry, two consumers. `RiftCanvas` draws it at rest behind her; `RiftReveal` opens
 * it and writes the same polygon to `clip-path` on the choose screen so what you see through
 * the crack is the other side. Both call `riftPolygon` with the same seed, so the crack she
 * guards is the crack that swallows you — not a lookalike.
 *
 * All randomness here is seeded. A crack that re-rolled its jaggedness on every mount would
 * jump between the idle canvas and the reveal overlay on the click frame.
 */

export interface RiftGeometry {
  /** Centre of the crack in viewport px. */
  x: number
  y: number
  /** Full length of the closed crack in px. */
  len: number
  /** Lean from vertical, radians. Negative leans the top to the left. */
  tilt: number
}

const VERTEX_COUNT = 30
const SEED = 0x5eed
/** Half-width of the closed seam at its widest, px. */
const SEAM = 4.6
/** Where the opening curve ends. Chosen so the polygon clears every viewport corner. */
export const K_MAX = 6
/** Fraction of the timeline spent cracking open before the pull begins. */
export const CRACK_UNTIL = 0.28

export function mulberry32(seed: number): () => number {
  let a = seed
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

interface Vertex {
  /** Position along the spine, -1 (top) → 1 (bottom). */
  s: number
  /** Sideways wobble of the spine, in units of len. */
  jitter: number
  /** Per-side width multipliers — what makes the edges jagged rather than a smooth lens. */
  jagL: number
  jagR: number
}

const VERTICES: Vertex[] = (() => {
  const rnd = mulberry32(SEED)
  return Array.from({ length: VERTEX_COUNT }, (_, i) => ({
    s: -1 + (2 * i) / (VERTEX_COUNT - 1),
    jitter: (rnd() - 0.5) * 0.034,
    jagL: 0.7 + rnd() * 0.5,
    jagR: 0.7 + rnd() * 0.5,
  }))
})()

export interface Branch {
  /** Spine position it leaves from. */
  s: number
  side: 1 | -1
  /** Successive offsets (along normal, along spine) in units of len. */
  steps: Array<[number, number]>
}

/** Fractures off the main seam — the wall is failing, not just split. */
export const BRANCHES: Branch[] = (() => {
  const rnd = mulberry32(SEED ^ 0x9e37)
  return Array.from({ length: 11 }, () => {
    const count = 3 + Math.floor(rnd() * 3)
    return {
      s: -0.85 + rnd() * 1.7,
      side: rnd() < 0.5 ? -1 : 1,
      steps: Array.from({ length: count }, () => [
        0.03 + rnd() * 0.06,
        (rnd() - 0.5) * 0.08,
      ]) as Array<[number, number]>,
    }
  })
})()

/**
 * A one-off tendril: a bolt that snaps off the seam and dies in a few frames. Unseeded on
 * purpose — these are transient, so they never have to match between canvases.
 */
export function randomTendril(): Branch {
  const count = 4 + Math.floor(Math.random() * 4)
  return {
    s: -0.9 + Math.random() * 1.8,
    side: Math.random() < 0.5 ? -1 : 1,
    steps: Array.from({ length: count }, () => [
      0.025 + Math.random() * 0.045,
      (Math.random() - 0.5) * 0.09,
    ]) as Array<[number, number]>,
  }
}

/** Lens profile: full width in the middle, pointed at both ends. */
const lens = (s: number): number => Math.pow(Math.max(0, 1 - s * s), 0.7)

/**
 * Outline of the crack at opening `k` (0 = closed seam), as a flat [x0, y0, x1, y1, …]
 * array, left edge top→bottom then right edge bottom→top.
 *
 * Opening does two things at once: the edges move apart (width ∝ k · reach) and the ends
 * stretch away along the spine (length ∝ 1 + 1.6k), so the hole grows as a lengthening
 * tear rather than a fattening oval.
 */
export function riftPolygon(g: RiftGeometry, k: number, reach: number): number[] {
  const dx = Math.sin(g.tilt)
  const dy = Math.cos(g.tilt)
  const nx = dy
  const ny = -dx
  const half = g.len / 2
  const stretch = 1 + k * 1.6
  // The teeth are a fraction of the width, so left alone they grow into hundreds of px as
  // the hole opens and the edge turns into a sawtooth. Damp them toward smooth as k grows:
  // full jag on the closed seam, a few percent once the tear is screen-sized.
  const jagDamp = 1 / (1 + k * 1.5)

  const left: number[] = []
  const right: number[] = []
  for (const v of VERTICES) {
    const along = v.s * half * stretch
    const wobble = v.jitter * g.len
    const px = g.x + dx * along + nx * wobble
    const py = g.y + dy * along + ny * wobble
    const l = lens(v.s)
    const base = SEAM * (0.25 + l) + k * reach * (0.12 + 0.88 * l)
    const hl = base * (1 + (v.jagL - 1) * jagDamp)
    const hr = base * (1 + (v.jagR - 1) * jagDamp)
    left.push(px - nx * hl, py - ny * hl)
    right.push(px + nx * hr, py + ny * hr)
  }
  const out = left
  for (let i = right.length - 2; i >= 0; i -= 2) out.push(right[i] as number, right[i + 1] as number)
  return out
}

/** A branch's polyline in viewport px, starting on the seam. */
export function branchPoints(g: RiftGeometry, b: Branch): number[] {
  const dx = Math.sin(g.tilt)
  const dy = Math.cos(g.tilt)
  const nx = dy * b.side
  const ny = -dx * b.side
  let x = g.x + dx * b.s * (g.len / 2)
  let y = g.y + dy * b.s * (g.len / 2)
  const pts = [x, y]
  for (const [out, run] of b.steps) {
    x += nx * out * g.len + dx * run * g.len
    y += ny * out * g.len + dy * run * g.len
    pts.push(x, y)
  }
  return pts
}

export function tracePath(ctx: CanvasRenderingContext2D, pts: number[], close: boolean): void {
  ctx.beginPath()
  ctx.moveTo(pts[0] as number, pts[1] as number)
  for (let i = 2; i < pts.length; i += 2) ctx.lineTo(pts[i] as number, pts[i + 1] as number)
  if (close) ctx.closePath()
}

export function polygonToClip(pts: number[]): string {
  const parts: string[] = []
  for (let i = 0; i < pts.length; i += 2) {
    parts.push(`${(pts[i] as number).toFixed(1)}px ${(pts[i + 1] as number).toFixed(1)}px`)
  }
  return `polygon(${parts.join(', ')})`
}

/**
 * Opening over time. Two beats:
 *  - crack (0 → CRACK_UNTIL): snaps to a hand's width and holds — the wall gives;
 *  - pull  (CRACK_UNTIL → 1): cubic ease-in to K_MAX — slow, then everything at once.
 * Raven's own animation is timed against the same marks in landing.css.
 */
export function openingAt(t: number): number {
  const crackK = 0.05
  if (t < CRACK_UNTIL) {
    const u = t / CRACK_UNTIL
    return crackK * (1 - Math.pow(1 - u, 3))
  }
  const u = (t - CRACK_UNTIL) / (1 - CRACK_UNTIL)
  return crackK + (K_MAX - crackK) * u * u * u
}

/**
 * Where the crack lives: behind Raven, centred a touch above her middle, a fifth taller
 * than she is so it runs past her head and feet, leaning so it shows past her
 * silhouette instead of hiding behind her spine. Capped to the stage so it never runs
 * under the header or footer chrome.
 */
/**
 * An element's box in viewport coordinates from LAYOUT ALONE — the offset chain, never
 * getBoundingClientRect.
 *
 * The distinction is the whole point: Raven carries a float animation, a hover scale and
 * the evasion drift, all of which move her measured rect every frame. The crack must not
 * follow any of them — she moves around it, not it around her. Offsets ignore transforms,
 * so this is where she IS rather than where she happens to be this frame.
 *
 * Returns null if the chain does not reach `root`, in which case the caller falls back to
 * the stage's own centre rather than adding offsets to the wrong origin.
 */
export function layoutBoxIn(el: HTMLElement, root: HTMLElement, rootRect: DOMRect): DOMRect | null {
  let x = 0
  let y = 0
  let n: HTMLElement | null = el
  while (n && n !== root) {
    x += n.offsetLeft
    y += n.offsetTop
    n = n.offsetParent as HTMLElement | null
  }
  if (n !== root) return null
  return new DOMRect(rootRect.left + x, rootRect.top + y, el.offsetWidth, el.offsetHeight)
}

export function riftFromLayout(stage: DOMRect, figure: DOMRect | null): RiftGeometry {
  const height = figure && figure.height > 0 ? figure.height : stage.height * 0.6
  // Centred on HER, not on the stage.
  //
  // These are the same point on a wide screen, where she is the only thing in the middle
  // of it — which is why the stage's centre stood in for hers for so long. They stop being
  // the same the moment anything else shares the column: on a portrait phone the name
  // stacks ZIN above her and HOS below, and the pitch sits under that, which lifts her
  // ~70px off the stage's centre. The crack is the one SHE is guarding, so it belongs
  // behind her wherever the layout puts her — otherwise it drifts out from under her feet.
  //
  // `figure` must therefore carry a position, not just a size (see layoutBoxIn); a rect at
  // the origin, or none at all, falls back to the stage.
  const positioned = figure !== null && figure.width > 0 && (figure.left !== 0 || figure.top !== 0)
  return {
    x: positioned ? figure.left + figure.width / 2 : stage.left + stage.width / 2,
    y: (positioned ? figure.top + figure.height / 2 : stage.top + stage.height / 2) - height * 0.03,
    len: Math.min(height * 1.2, stage.height * 0.98),
    tilt: -0.21,
  }
}

/** Distance from the crack centre to the farthest viewport corner. */
export function reachOf(g: RiftGeometry, w: number, h: number): number {
  return Math.hypot(Math.max(g.x, w - g.x), Math.max(g.y, h - g.y))
}
