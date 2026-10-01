/**
 * The app's audio: two decks, a crossfader, and one analyser behind them.
 *
 * A singleton for the same reason `cursor` is one: the spectrum canvases sample it every
 * animation frame, and routing per-frame audio data through React state would re-render the
 * console sixty times a second for a value only a canvas reads. React subscribes to the
 * coarse state (which track, playing or not) through `usePlayer`; the canvases pull the fine
 * state (`spectrum`, `level`, `progress`) themselves.
 *
 * Two decks, not one, because Drift mixes: the next song rises while this one falls, and a
 * single element can only cut. Each deck is `<audio> → gain`, both into one analyser, so
 * every spectrum in the app hears exactly what the listener hears — both songs, mid-mix.
 * A MediaElementSource can be made only once per element, so the decks are built once and
 * reused; a change of song is a new `src` on the deck not currently live.
 *
 * Every element event is ignored unless it comes from the LIVE deck. The outgoing deck is
 * paused after its fade and eventually ends, and either event, taken at face value, would
 * report the new song as paused or push the queue on.
 *
 * The previews are Spotify's 30-second clips, served from p.scdn.co with
 * `Access-Control-Allow-Origin: *`. That header is what makes the analyser possible at all:
 * a MediaElementSource over a cross-origin element WITHOUT it outputs silence (the browser
 * zeroes the samples rather than leak them), so `crossOrigin = 'anonymous'` is set before
 * the first `src` and never removed. The catalogue is sparse — a good share of tracks carry
 * no preview_url — so the queue skips those rather than stopping on them.
 */
import { useSyncExternalStore } from 'react'
import type { TrackRecord } from './api'

export interface PlayerState {
  track: TrackRecord | null
  playing: boolean
  /** True between asking the element to play and its first `playing` event. */
  buffering: boolean
  /** The list the current track was started from; `ended` advances through it. */
  queue: readonly TrackRecord[]
  index: number
  error: string | null
}

const IDLE: PlayerState = {
  track: null,
  playing: false,
  buffering: false,
  queue: [],
  index: -1,
  error: null,
}

let state: PlayerState = IDLE
const listeners = new Set<() => void>()

const set = (patch: Partial<PlayerState>): void => {
  state = { ...state, ...patch }
  listeners.forEach((fn) => fn())
}

// ── The decks ────────────────────────────────────────────────────────────────────────
// The graph is built lazily on the first play, because an AudioContext created before a
// user gesture starts suspended and Chrome logs a warning for every one made on page load.

interface Deck {
  el: HTMLAudioElement
  gain: GainNode | null
  /** onNearEnd has fired for the clip on this deck. */
  warned: boolean
}

let ctx: AudioContext | null = null
let analyser: AnalyserNode | null = null
let bins: Uint8Array<ArrayBuffer> | null = null
/**
 * After the analyser, before the speakers: a lowpass that is wide open except while Drift
 * is being pulled into the rift, when the music goes under. After the analyser on purpose —
 * the spectra keep showing the music, only the ear hears it muffled.
 */
let muffler: BiquadFilterNode | null = null
let noise: AudioBuffer | null = null
let decks: [Deck, Deck] | null = null
let live = 0

/** A console play fades in over this long. A clip that starts at full level mid-phrase clicks. */
const FADE_S = 0.35
/** How long before a clip ends `onNearEnd` fires — the mix length Drift uses at a song's end. */
export const NEAR_END_S = 3.2

const isLive = (d: Deck): boolean => decks?.[live] === d

function makeDeck(): Deck {
  const el = new Audio()
  el.crossOrigin = 'anonymous'
  el.preload = 'auto'
  const d: Deck = { el, gain: null, warned: false }
  el.addEventListener('playing', () => isLive(d) && set({ playing: true, buffering: false }))
  el.addEventListener('pause', () => isLive(d) && set({ playing: false }))
  el.addEventListener('timeupdate', () => {
    if (!isLive(d) || d.warned || !player.onNearEnd) return
    const dur = el.duration
    if (dur && Number.isFinite(dur) && dur - el.currentTime < NEAR_END_S + 0.4) {
      d.warned = true
      player.onNearEnd()
    }
  })
  el.addEventListener('ended', () => {
    if (!isLive(d)) return
    // Someone is steering (Drift): the end is theirs to handle. Otherwise, the queue.
    if (player.onNearEnd) {
      if (!d.warned) {
        d.warned = true
        player.onNearEnd()
      }
    } else start(state.index + 1)
  })
  el.addEventListener('error', () => {
    // A dead preview link is a property of that one track, not of the player: note it and
    // move on down the queue, the way a radio skips a bad cart.
    if (!isLive(d) || !state.track) return
    set({ error: `preview unavailable for “${state.track.title}”` })
    if (player.onNearEnd) player.onNearEnd()
    else start(state.index + 1)
  })
  return d
}

function graph(): [Deck, Deck] {
  if (decks && ctx) return decks
  const pair: [Deck, Deck] = [makeDeck(), makeDeck()]
  const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
  if (Ctor) {
    ctx = new Ctor()
    analyser = ctx.createAnalyser()
    // 512 → 256 bins of ~86 Hz at 44.1 kHz. The seams draw 24-48 points, so more resolution
    // would only be averaged away, and a smaller FFT reacts faster to transients.
    analyser.fftSize = 512
    analyser.smoothingTimeConstant = 0.8
    muffler = ctx.createBiquadFilter()
    muffler.type = 'lowpass'
    muffler.frequency.value = 20000
    muffler.Q.value = 0.9
    analyser.connect(muffler)
    muffler.connect(ctx.destination)
    for (const d of pair) {
      d.gain = ctx.createGain()
      d.gain.gain.value = 0
      ctx.createMediaElementSource(d.el).connect(d.gain)
      d.gain.connect(analyser)
    }
    bins = new Uint8Array(analyser.frequencyBinCount)
  }
  decks = pair
  return pair
}

/** Ramp a deck's gain from wherever it is. Ramps, not value curves: curves may not overlap,
 *  and a second switch inside one fade would overlap them and throw. */
function ramp(d: Deck, to: number, secs: number): void {
  if (!d.gain || !ctx) {
    d.el.volume = to
    return
  }
  const g = d.gain.gain
  const now = ctx.currentTime
  const v = g.value
  g.cancelScheduledValues(now)
  g.setValueAtTime(v, now)
  g.linearRampToValueAtTime(to, now + Math.max(0.02, secs))
}

const playable = (t: TrackRecord | undefined): t is TrackRecord => !!t?.preview_url

/**
 * Put `t` on the idle deck and cross over to it: the new deck rises from silence over
 * `fade`, the old one falls over the same time and is paused once it is quiet.
 */
function crossTo(t: TrackRecord, fade: number): void {
  const [a, b] = graph()
  void ctx?.resume()
  const from = live === 0 ? a : b
  const to = live === 0 ? b : a
  live = live === 0 ? 1 : 0
  to.warned = false
  to.el.src = t.preview_url ?? ''
  // From true silence: this deck may be the one still fading out from a switch a moment ago.
  if (to.gain && ctx) {
    const g = to.gain.gain
    g.cancelScheduledValues(ctx.currentTime)
    g.setValueAtTime(0, ctx.currentTime)
    g.linearRampToValueAtTime(1, ctx.currentTime + Math.max(0.02, fade))
  } else to.el.volume = 1
  if (!from.el.paused) {
    ramp(from, 0, fade)
    const el = from.el
    window.setTimeout(() => {
      if (decks?.[live]?.el !== el) el.pause()
    }, fade * 1000 + 80)
  }
  set({ track: t, buffering: true, error: null, playing: false })
  to.el.play().catch((e: unknown) => {
    // AbortError is the previous play() being superseded by this one — expected when the
    // listener clicks down a list quickly. Anything else is real.
    if (e instanceof DOMException && e.name === 'AbortError') return
    set({ buffering: false, playing: false, error: 'the browser refused to play this preview' })
  })
}

/** Begin the first playable track at or after `i`; stop quietly when the queue runs out. */
function start(i: number): void {
  const q = state.queue
  let j = i
  while (j < q.length && !playable(q[j])) j++
  const t = q[j]
  if (!playable(t)) {
    stop()
    return
  }
  state = { ...state, index: j }
  crossTo(t, FADE_S)
}

const liveEl = (): HTMLAudioElement | null => decks?.[live]?.el ?? null

// ── Public controls ──────────────────────────────────────────────────────────────────

export const player = {
  /**
   * Set while something else steers the music (Drift): called once per clip, NEAR_END_S
   * before it runs out, so the next song can be mixed in; and the queue no longer
   * advances on its own. Cleared again (null) when that something is done.
   */
  onNearEnd: null as (() => void) | null,
  /** Set alongside onNearEnd: what "next" means while something else steers (Drift's next song). */
  onNext: null as (() => void) | null,

  /**
   * Play `list[i]` and carry on through the rest of `list`. The same track again toggles
   * pause instead, so a row is its own play/pause button.
   */
  play(list: readonly TrackRecord[], i: number): void {
    const t = list[i]
    if (!t) return
    if (state.track?.track_id === t.track_id && liveEl()?.src) {
      player.toggle()
      return
    }
    state = { ...state, queue: list }
    start(i)
  },

  /**
   * Mix to one track over `fade` seconds — Drift's move. The same track already on air is
   * left alone (no restart), so entering Drift from the song you were playing is seamless.
   */
  mix(t: TrackRecord, fade: number): void {
    if (!playable(t)) return
    state = { ...state, queue: [t], index: 0 }
    if (state.track?.track_id === t.track_id && liveEl()?.src) {
      const el = liveEl()!
      if (el.paused) player.toggle()
      // Rearm the near-end call for this clip: whoever steers now has not heard it yet.
      if (decks) decks[live]!.warned = false
      set({})
      return
    }
    crossTo(t, fade)
  },

  toggle(): void {
    const el = liveEl()
    if (!el || !state.track) return
    if (el.paused) {
      void ctx?.resume()
      void el.play()
      if (decks) ramp(decks[live]!, 1, 0.15)
    } else el.pause()
  },
  next(): void {
    if (player.onNext) player.onNext()
    else if (state.track) start(state.index + 1)
  },
  prev(): void {
    const el = liveEl()
    if (!state.track || !el) return
    // Past the first few seconds, "back" means "from the top", as on every player.
    if (el.currentTime > 3) {
      el.currentTime = 0
      return
    }
    let j = state.index - 1
    while (j >= 0 && !playable(state.queue[j])) j--
    if (j >= 0) start(j)
    else el.currentTime = 0
  },
  /** Stop and forget the queue. Called when the matrix unmounts: the music stays in the rift. */
  stop,
  /** 0-1 through the current clip. */
  progress(): number {
    const el = liveEl()
    if (!el || !el.duration || !Number.isFinite(el.duration)) return 0
    return el.currentTime / el.duration
  },
  /** Seconds in, and total, of the live clip. */
  time(): [number, number] {
    const el = liveEl()
    const d = el?.duration
    return [el?.currentTime ?? 0, d && Number.isFinite(d) ? d : 30]
  },
  /**
   * The live spectrum, 0-255 per bin, or null before anything has played. The array is
   * reused between calls — read it, do not keep it.
   */
  spectrum(): Uint8Array | null {
    if (!analyser || !bins) return null
    analyser.getByteFrequencyData(bins)
    return bins
  },
  /** Low-end energy, 0-1: the average of the bottom ~500 Hz, where the kick and bass live. */
  level(): number {
    const b = player.spectrum()
    if (!b) return 0
    let s = 0
    for (let i = 1; i < 7; i++) s += b[i] ?? 0
    return s / (6 * 255)
  },
  get state(): PlayerState {
    return state
  },
}

// ── The fold's sound ─────────────────────────────────────────────────────────────────

function noiseBuffer(c: AudioContext): AudioBuffer {
  if (noise) return noise
  noise = c.createBuffer(1, Math.round(c.sampleRate * 1.5), c.sampleRate)
  const d = noise.getChannelData(0)
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1
  return noise
}

/**
 * Drift folding into the rift and tearing back out of it, as sound. Every call is a no-op
 * until the graph exists — nothing here should be the first thing to make an AudioContext.
 */
export const fx = {
  /** Sweep the music's lowpass to `hz` over `secs` (exponential: it is what the ear hears as even). */
  muffle(hz: number, secs: number): void {
    if (!ctx || !muffler) return
    const f = muffler.frequency
    const now = ctx.currentTime
    f.cancelScheduledValues(now)
    f.setValueAtTime(Math.max(40, f.value), now)
    f.exponentialRampToValueAtTime(Math.max(40, hz), now + Math.max(0.02, secs))
  },
  /**
   * Air rushing: noise through a bandpass whose centre sweeps. `rising` for being pulled in
   * (low to high, swelling), falling for the burst back out.
   */
  whoosh(secs: number, rising: boolean): void {
    if (!ctx) return
    const now = ctx.currentTime
    const src = ctx.createBufferSource()
    src.buffer = noiseBuffer(ctx)
    const bp = ctx.createBiquadFilter()
    bp.type = 'bandpass'
    bp.Q.value = 1.4
    bp.frequency.setValueAtTime(rising ? 220 : 3200, now)
    bp.frequency.exponentialRampToValueAtTime(rising ? 3600 : 180, now + secs)
    const g = ctx.createGain()
    g.gain.setValueAtTime(0.0001, now)
    if (rising) {
      g.gain.exponentialRampToValueAtTime(0.32, now + secs * 0.9)
      g.gain.exponentialRampToValueAtTime(0.0001, now + secs + 0.05)
    } else {
      g.gain.exponentialRampToValueAtTime(0.34, now + 0.04)
      g.gain.exponentialRampToValueAtTime(0.0001, now + secs)
    }
    src.connect(bp)
    bp.connect(g)
    g.connect(ctx.destination)
    src.start(now)
    src.stop(now + secs + 0.1)
  },
  /** The landing: a sub drop under a short crack of noise. */
  thud(): void {
    if (!ctx) return
    const now = ctx.currentTime
    const o = ctx.createOscillator()
    const og = ctx.createGain()
    o.type = 'sine'
    o.frequency.setValueAtTime(95, now)
    o.frequency.exponentialRampToValueAtTime(32, now + 0.45)
    og.gain.setValueAtTime(0.0001, now)
    og.gain.exponentialRampToValueAtTime(0.75, now + 0.012)
    og.gain.exponentialRampToValueAtTime(0.0001, now + 0.55)
    o.connect(og)
    og.connect(ctx.destination)
    o.start(now)
    o.stop(now + 0.6)
    const n = ctx.createBufferSource()
    n.buffer = noiseBuffer(ctx)
    const hp = ctx.createBiquadFilter()
    hp.type = 'highpass'
    hp.frequency.value = 1800
    const ng = ctx.createGain()
    ng.gain.setValueAtTime(0.35, now)
    ng.gain.exponentialRampToValueAtTime(0.0001, now + 0.08)
    n.connect(hp)
    hp.connect(ng)
    ng.connect(ctx.destination)
    n.start(now)
    n.stop(now + 0.1)
  },
}

function stop(): void {
  for (const d of decks ?? []) {
    d.el.pause()
    d.el.removeAttribute('src')
    d.el.load()
    if (d.gain && ctx) d.gain.gain.setValueAtTime(0, ctx.currentTime)
  }
  set({ ...IDLE, error: state.error })
}

const subscribe = (fn: () => void): (() => void) => {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** The coarse player state, for rendering. Per-frame values come from `player.*` directly. */
export function usePlayer(): PlayerState {
  return useSyncExternalStore(subscribe, () => state)
}

/** Spotify serves every cover at three sizes, keyed by one segment of the path. */
export function coverAt(url: string | null, size: 64 | 300 | 640): string | null {
  if (!url) return null
  const key = size === 64 ? 'ab67616d00004851' : size === 300 ? 'ab67616d00001e02' : 'ab67616d0000b273'
  return url.replace(/ab67616d0000(?:b273|1e02|4851)/, key)
}
