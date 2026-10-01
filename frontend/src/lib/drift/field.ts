/**
 * The paint: N particles, each pulled home to one pixel of the current picture and pushed
 * off it by the music.
 *
 * Three forces, all per particle, all cheap enough to run 12,544 times a frame in JS:
 *
 *   spring   back to its home pixel. At silence this wins and the cover is simply there,
 *            as a pointillist painting.
 *   flow     a divergence-free swirl — the curl of an analytic stream function — so paint
 *            moves in eddies instead of spraying. Its strength per particle is that
 *            particle's band of the spectrum: the centre of the picture listens to the
 *            bass, the rim to the treble, so a hi-hat frays the edges and a kick moves the
 *            middle.
 *   kick     a radial shove on a bass onset, so the painting visibly takes the beat.
 *
 * A new picture does not arrive all at once: each particle leaves on its own small delay,
 * and its colour eases to the new pixel over the flight.
 */
import { N, type Source } from './source'

/** A disc the paint pours out of: centre and radius, in painting space (−1…1, y down). */
export interface Pour {
  x: number
  y: number
  r: number
}

export class Field {
  readonly px = new Float32Array(N)
  readonly py = new Float32Array(N)
  private vx = new Float32Array(N)
  private vy = new Float32Array(N)
  private hx = new Float32Array(N)
  private hy = new Float32Array(N)
  private nx = new Float32Array(N)
  private ny = new Float32Array(N)
  private leaveAt = new Float32Array(N)
  private band = new Uint16Array(N)
  private from = new Float32Array(N * 4)
  private to = new Float32Array(N * 4)
  private mixT = new Float32Array(N)

  /** Interleaved for the GPU: x, y per particle, and r, g, b, a. */
  readonly pos = new Float32Array(N * 2)
  readonly col = new Float32Array(N * 4)
  colourDirty = true

  /**
   * Where the next picture's paint pours from, in painting space (the chosen option's disc),
   * or null for the plain rearrangement. And how fast its colour comes up once poured.
   */
  private origin: Pour | null = null
  private mixRate = 1.1

  /** Extra swirl while a picture is changing, decaying. */
  private stir = 0
  private kick = 0
  private bassSlow = 0
  private clock = 0

  /** `from`: pour the first picture out of that disc (the row it was started from). */
  constructor(first: Source, from: Pour | null = null) {
    for (let i = 0; i < N; i++) {
      // Start scattered, so the first thing anyone sees is the word condensing out of dust.
      const a = Math.random() * Math.PI * 2
      const r = 0.6 + Math.random() * 1.2
      this.px[i] = Math.cos(a) * r
      this.py[i] = Math.sin(a) * r
      this.col[i * 4 + 3] = 0
    }
    this.set(first, from ? 0.7 : 1.2, from)
  }

  /**
   * Change what the paint is becoming. `spread` is the window, in seconds, particles leave
   * over. With `from`, each particle, when its turn comes, leaves the old picture and
   * re-enters from that disc — so the new cover pours out of the song you chose, while the
   * old one erodes as its paint goes and dries on the page.
   */
  set(s: Source, spread = 0.9, from: Pour | null = null): void {
    const now = this.clock
    this.origin = from
    this.mixRate = from ? 5 : 1.8
    for (let j = 0; j < N; j++) {
      this.nx[j] = s.x[j]!
      this.ny[j] = s.y[j]!
      // Dark paint leaves first: shadows move, then the midtones, then the light.
      this.leaveAt[j] = now + (j / N) * spread * 0.6 + Math.random() * spread * 0.4
      const k = j * 4
      this.from[k] = this.col[k]!
      this.from[k + 1] = this.col[k + 1]!
      this.from[k + 2] = this.col[k + 2]!
      this.from[k + 3] = this.col[k + 3]!
      this.to[k] = s.r[j]!
      this.to[k + 1] = s.g[j]!
      this.to[k + 2] = s.b[j]!
      this.to[k + 3] = s.a[j]!
      this.mixT[j] = -1 // not started
    }
    // A pour is a jet: less swirl, so it reads as coming FROM somewhere. A rearrangement
    // has nowhere to come from, and the swirl is what carries it.
    this.stir = from ? 0.45 : 1
  }

  /**
   * One frame. `bins` is the live spectrum (0-255, may be empty), `calm` 0-1 turns the
   * music's effect down (reduced motion, or paused).
   */
  step(dt: number, bins: Uint8Array, calm: number): void {
    this.clock += dt
    const t = this.clock
    const nb = bins.length
    const live = nb > 0 ? 1 - calm : 0

    // Bass onset → kick. The slow average is the floor a hit must rise above.
    let bass = 0
    if (nb) {
      for (let i = 1; i < 6; i++) bass += bins[i]!
      bass /= 5 * 255
    }
    if (bass - this.bassSlow > 0.07 && live > 0) this.kick = Math.min(1.4, this.kick + (bass - this.bassSlow) * 5)
    this.bassSlow += (bass - this.bassSlow) * Math.min(1, dt * 3)
    this.kick *= Math.exp(-dt * 7)
    this.stir *= Math.exp(-dt * 1.8)

    const damp = Math.exp(-dt * 3.4)
    const K = 10
    // Stream-function constants. ψ = A sin(ax + w1 t) cos(by − w2 t) + B sin(c(x + y) + w3 t)
    const a = 2.1
    const b = 1.7
    const c = 3.3
    const w1 = t * 0.31
    const w2 = t * 0.23
    const w3 = t * 0.47
    const col = this.col
    let dirty = false

    for (let i = 0; i < N; i++) {
      if (this.mixT[i]! < 0 && t >= this.leaveAt[i]!) {
        this.hx[i] = this.nx[i]!
        this.hy[i] = this.ny[i]!
        const r = Math.sqrt(this.hx[i]! * this.hx[i]! + this.hy[i]! * this.hy[i]!)
        this.band[i] = 1 + Math.floor(Math.pow(Math.min(1, r / 1.3), 1.4) * 96)
        this.mixT[i] = 0
        const o = this.origin
        if (o) {
          // Out of the disc, anywhere across its face, sprayed toward home with some scatter;
          // the spring and the swirl then bring it in exactly as before.
          const a = Math.random() * Math.PI * 2
          const rr = Math.sqrt(Math.random()) * o.r
          const x0 = o.x + Math.cos(a) * rr
          const y0 = o.y + Math.sin(a) * rr
          const dx = this.hx[i]! - x0
          const dy = this.hy[i]! - y0
          const d = Math.sqrt(dx * dx + dy * dy) + 1e-3
          const sp = 3 + Math.random() * 2.4
          this.px[i] = x0
          this.py[i] = y0
          this.vx[i] = (dx / d) * sp + (Math.random() - 0.5) * 0.55
          this.vy[i] = (dy / d) * sp + (Math.random() - 0.5) * 0.55
          // Already the new song's colour; it only has to fade up out of the disc.
          const k = i * 4
          this.from[k] = this.to[k]!
          this.from[k + 1] = this.to[k + 1]!
          this.from[k + 2] = this.to[k + 2]!
          this.from[k + 3] = 0
        }
      }
      const m = this.mixT[i]!
      if (m >= 0 && m < 1) {
        const nm = Math.min(1, m + dt * this.mixRate)
        this.mixT[i] = nm
        const e = nm * nm * (3 - 2 * nm)
        const k = i * 4
        col[k] = this.from[k]! + (this.to[k]! - this.from[k]!) * e
        col[k + 1] = this.from[k + 1]! + (this.to[k + 1]! - this.from[k + 1]!) * e
        col[k + 2] = this.from[k + 2]! + (this.to[k + 2]! - this.from[k + 2]!) * e
        col[k + 3] = this.from[k + 3]! + (this.to[k + 3]! - this.from[k + 3]!) * e
        dirty = true
      }

      const x = this.px[i]!
      const y = this.py[i]!
      // ∂ψ/∂y and −∂ψ/∂x, analytically.
      const s1 = Math.sin(a * x + w1)
      const c1 = Math.cos(a * x + w1)
      const s2 = Math.sin(b * y - w2)
      const c2 = Math.cos(b * y - w2)
      const c3 = Math.cos(c * (x + y) + w3)
      const u = -s1 * s2 * b + 0.6 * c3 * c
      const v = -(c1 * c2 * a + 0.6 * c3 * c)

      const react = nb ? bins[this.band[i]!]! / 255 : 0
      // Tuned against the particle spacing (2/112 ≈ 0.018 units): at rest the swirl moves a
      // particle about half a cell, so a silent cover is crisp; a loud band moves it a
      // third of the painting, which smears it without losing it.
      const amp = 0.02 + this.stir * 1.6 + react * react * 2.4 * live

      let fx = K * (this.hx[i]! - x) + u * amp
      let fy = K * (this.hy[i]! - y) + v * amp
      if (this.kick > 0.01) {
        const r = Math.sqrt(x * x + y * y) + 0.05
        const push = this.kick * 5 * (0.4 + Math.min(1, r))
        fx += (x / r) * push
        fy += (y / r) * push
      }
      const nvx = (this.vx[i]! + fx * dt) * damp
      const nvy = (this.vy[i]! + fy * dt) * damp
      this.vx[i] = nvx
      this.vy[i] = nvy
      this.px[i] = x + nvx * dt
      this.py[i] = y + nvy * dt
      this.pos[i * 2] = this.px[i]!
      this.pos[i * 2 + 1] = this.py[i]!
    }
    if (dirty) this.colourDirty = true
  }

  /** How settled the paint is, 0 still - 1 wild. Drives how fast the canvas dries. */
  get agitation(): number {
    return Math.min(1, this.stir + this.kick * 0.5)
  }
}
