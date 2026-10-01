/**
 * Drift's radio, without the page: which song is on, where it could go next, the path so
 * far, and the painting that shows it. React renders `view`; everything per-frame (the
 * paint, the progress line, the ring) runs here in one rAF, outside React, the way
 * `cursor.ts` and `player.ts` keep their per-frame values out of state.
 *
 * Ported from frontend-drift/src/main.ts, which stays the standalone version. The music
 * goes through the app's own player (lib/player.ts), not a deck of its own: one audio
 * graph for the whole app, so the rift, the pill and the console all hear what Drift plays.
 */
import { api, EngineError, type TrackRecord } from '../api'
import { coverAt, NEAR_END_S, player } from '../player'
import { Field, type Pour } from './field'
import { Painter, type RGB } from './painter'
import { fromImage, fromSeed, type Source } from './source'

export interface Way {
  track: TrackRecord
  label: string
  score: number
}

export interface Hop {
  track: TrackRecord
  /** Similarity to the song before it; null for the seed. */
  score: number | null
}

export interface RadioView {
  path: Hop[]
  at: number
  ways: Way[]
  /** Which way the radio takes on its own when the song runs out; -1 until ways arrive. */
  auto: number
  /** The song ran out before the ways arrived: go the moment they do. */
  owed: boolean
  /** The current cover's colour, lifted to read on night. */
  accent: string
  note: string | null
}

/** The portal's ground (--bg, #0c0814), which the paint sits on and dries back to. */
export const GROUND: RGB = [0.047, 0.031, 0.078]
/** A chosen switch mixes quickly; the radio's own drift at the end of a song takes its time. */
const QUICK = 1

const fold = (s: string): string =>
  s
    .toLowerCase()
    .replace(/\s*[([].*?[)\]]/g, '')
    .replace(/\s+-\s+.*$/, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
/** Same song, another pressing: remasters, live cuts and "feat." credits all fold together. */
const songKey = (t: TrackRecord): string => `${fold(t.title)}|${fold((t.artists ?? '').split(',')[0] ?? '')}`
const firstArtist = (t: TrackRecord): string => fold((t.artists ?? '').split(',')[0] ?? '')

/**
 * The source's accent is tuned for ink on paper (lightness 36-50%). On night it needs to be
 * light instead, so the hue and saturation are kept and the lightness lifted.
 */
function forNight(accent: string): string {
  const m = accent.match(/hsl\((\d+)\s+(\d+)%\s+\d+%\)/)
  return m ? `hsl(${m[1]} ${Math.max(60, Number(m[2]))}% 72%)` : 'hsl(270 80% 75%)'
}

export class Radio {
  view: RadioView = { path: [], at: -1, ways: [], auto: -1, owed: false, accent: 'hsl(270 80% 75%)', note: null }
  /** Where a way's disc is on screen, supplied by the page — the pour starts from it. */
  locate: ((i: number) => DOMRect | null) | null = null
  /** Per-frame hook for the page's progress line and ring. */
  onFrame: ((cur: number, dur: number) => void) | null = null

  private painter: Painter
  private field: Field | null = null
  private listeners = new Set<() => void>()
  private epoch = 0
  private played = new Set<string>()
  private reserve: TrackRecord[] = []
  private sources = new Map<number, Promise<Source>>()
  private raf = 0
  private last = performance.now()
  private reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  /**
   * Minimized: the radio keeps choosing and mixing, but nothing is drawn — the screen is
   * folded away into the rift. The canvas keeps its last frame, which is what unfolds.
   */
  hidden = false

  constructor(canvas: HTMLCanvasElement) {
    this.painter = new Painter(canvas, { ground: GROUND, grain: 0.5 })
    player.onNearEnd = () => this.nearEnd()
    player.onNext = () => this.go(this.view.auto)
    this.raf = requestAnimationFrame(this.frame)
  }

  // ── Subscription, for useSyncExternalStore ─────────────────────────────────────────
  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }
  getView = (): RadioView => this.view
  private emit(patch: Partial<RadioView>): void {
    this.view = { ...this.view, ...patch }
    this.listeners.forEach((fn) => fn())
  }

  // ── Geometry ───────────────────────────────────────────────────────────────────────
  layout(cx: number, cy: number, half: number): void {
    this.painter.layout = { cx, cy, half }
  }
  resize(): void {
    this.painter.resize()
  }
  /** A screen rectangle (a disc, a thumbnail, a console row's cover) as a pour origin. */
  pourFrom(r: DOMRect | null): Pour | null {
    if (!r) return null
    const L = this.painter.layout
    return { x: (r.left + r.width / 2 - L.cx) / L.half, y: (r.top + r.height / 2 - L.cy) / L.half, r: (r.width / 2) * 0.8 / L.half }
  }

  // ── Playing ────────────────────────────────────────────────────────────────────────

  /** Each song's picture, decoded once; the ways on are fetched as soon as they are offered. */
  private sourceFor(t: TrackRecord): Promise<Source> {
    let p = this.sources.get(t.track_id)
    if (!p) {
      const art = coverAt(t.cover_art_url, 300)
      p = (art ? fromImage(art) : Promise.resolve(null)).then((s) => s ?? fromSeed(t.track_id))
      this.sources.set(t.track_id, p)
      if (this.sources.size > 80) this.sources.delete(this.sources.keys().next().value!)
    }
    return p
  }

  /** Start from `seed`, poured out of `from` (the row it was chosen in) if given. */
  start(seed: TrackRecord, from: Pour | null): void {
    this.play(seed, null, from, 0.6)
  }

  /** Take way `i`. `fade` is the music's mix; a click is quick, the song's own end is not. */
  go(i: number, fade = QUICK): void {
    const w = this.view.ways[i]
    if (!w) return
    this.play(w.track, w.score, this.pourFrom(this.locate?.(i) ?? null), fade)
  }

  /** Branch from an earlier song on the path: cut the path there and carry on from it. */
  branch(i: number, from: DOMRect | null): void {
    const hop = this.view.path[i]
    if (!hop || i === this.view.at) return
    this.played.delete(songKey(hop.track))
    this.view = { ...this.view, at: i - 1 }
    this.play(hop.track, hop.score, this.pourFrom(from), QUICK)
  }

  private play(t: TrackRecord, score: number | null, from: Pour | null, fade: number): void {
    if (!t.preview_url) return
    const e = ++this.epoch
    const path = [...this.view.path.slice(0, this.view.at + 1), { track: t, score }]
    this.played.add(songKey(t))
    this.reserve = this.reserve.filter((r) => songKey(r) !== songKey(t))
    this.emit({ path, at: path.length - 1, ways: [], auto: -1, owed: false, note: null })
    player.mix(t, fade)

    void this.sourceFor(t).then((s) => {
      if (e !== this.epoch) return
      if (!this.field) this.field = new Field(s, from)
      else this.field.set(s, from ? 0.7 : 0.6, from)
      this.emit({ accent: forNight(s.accent) })
    })
    void this.findWays(t, e)
  }

  private nearEnd(): void {
    const v = this.view
    if (v.ways[v.auto]) this.go(v.auto, NEAR_END_S)
    else this.emit({ owed: true })
  }

  private async findWays(t: TrackRecord, e: number): Promise<void> {
    let got: TrackRecord[] = []
    try {
      got = (await api.similar(t.track_id, 24)).results
    } catch (err) {
      if (e === this.epoch) this.emit({ note: err instanceof EngineError ? err.message : 'lost the signal for a moment' })
    }
    if (e !== this.epoch) return

    const fresh = (x: TrackRecord) => !!x.preview_url && !this.played.has(songKey(x))
    const pool: TrackRecord[] = []
    const seen = new Set<string>()
    const take = (x: TrackRecord) => {
      const k = songKey(x)
      if (fresh(x) && !seen.has(k)) {
        seen.add(k)
        pool.push(x)
      }
    }
    got.forEach(take)
    // Too few of its own? Borrow from neighbours of earlier songs that were never taken.
    for (const x of this.reserve) {
      if (pool.length >= 8) break
      take(x)
    }
    if (pool.length === 0) {
      this.emit({ note: 'this one sits alone — nothing unplayed nearby. Branch from the path below.' })
      return
    }

    const n = pool.length
    const slots: Array<[number, string]> = [
      [0, 'closest'],
      [Math.min(2, n - 1), 'nearby'],
      [Math.floor(n * 0.5), 'further out'],
      [n - 1, 'a leap'],
    ]
    const used = new Set<number>()
    const ways: Way[] = []
    for (const [i, label] of slots) {
      let j = i
      while (used.has(j) && j < n - 1) j++
      while (used.has(j) && j > 0) j--
      if (used.has(j)) continue
      used.add(j)
      const tr = pool[j]!
      ways.push({ track: tr, label, score: tr.score ?? 0 })
    }
    this.reserve = [...pool.filter((_, j) => !used.has(j)), ...this.reserve].slice(0, 40)
    ways.forEach((w) => void this.sourceFor(w.track))

    // The radio leans "nearby" — close enough to hold the mood, far enough to move — but not
    // onto the same artist twice running, which is where pure nearest-neighbour radio sticks.
    const here = firstArtist(t)
    const pref = [1, 2, 0, 3]
    const auto = pref.find((i) => ways[i] && firstArtist(ways[i]!.track) !== here) ?? pref.find((i) => ways[i]) ?? 0
    const owed = this.view.owed
    this.emit({ ways, auto })
    // Owed means the last song already ran out: nothing left to mix against, so no long fade.
    if (owed) this.go(auto)
  }

  // ── The frame ──────────────────────────────────────────────────────────────────────
  private frame = (ts: number): void => {
    const dt = Math.min(0.05, (ts - this.last) / 1000)
    this.last = ts
    if (this.hidden) {
      this.raf = requestAnimationFrame(this.frame)
      return
    }
    if (this.field) {
      const bins = player.spectrum() ?? new Uint8Array(0)
      this.field.step(dt, bins, this.reduced ? 0.75 : player.state.playing ? 0 : 1)
      // Wild paint dries slowly and leaves long strokes; settled paint dries fast and stays crisp.
      this.painter.draw(this.field, 0.075 - this.field.agitation * 0.05)
    }
    const [cur, dur] = player.time()
    this.onFrame?.(cur, dur)
    this.raf = requestAnimationFrame(this.frame)
  }

  dispose(): void {
    cancelAnimationFrame(this.raf)
    this.epoch++
    player.onNearEnd = null
    player.onNext = null
    this.painter.dispose()
    this.listeners.clear()
  }
}
