import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { TrackRecord } from '../lib/api'
import { coverAt, NEAR_END_S, player, usePlayer } from '../lib/player'
import { Radio, type RadioView } from '../lib/drift/radio'
import { impact, runFold, type FoldRun, type FoldTarget } from '../lib/drift/fold'
import '../styles/drift.css'

/**
 * Drift, inside the portal: the listening mode every door leads to.
 *
 * Any track in the console (or the pill) can be drifted from. Its cover pours out of the row
 * it was chosen in and becomes the painting; from then on the radio never stops — each song
 * is followed by one of its own neighbours, four offered beside the painting (closest just
 * off the paint, a leap well clear), and the chosen one's paint pours out of its disc into
 * the picture. The path along the foot is every song so far; any of it can be branched from.
 *
 * The engine of it is lib/drift (radio, field, painter, source — the painter here on the
 * portal's night ground instead of paper). This component only lays it out. The standalone,
 * paper version lives on in frontend-drift/.
 */

interface Props {
  seed: TrackRecord
  /** Where the seed was chosen on screen (its cover in the console row); the first pour starts there. */
  from: DOMRect | null
  /** Folded away into the rift: the radio plays on, the screen is gone. */
  minimized: boolean
  onMinimize: () => void
  /**
   * Whether Drift fully covers the screen right now. Only then may the matrix underneath stop
   * painting — mid-fold, the console must still be there around the band.
   */
  onCover: (covered: boolean) => void
}

const EMPTY: RadioView = { path: [], at: -1, ways: [], auto: -1, owed: false, accent: 'hsl(270 80% 75%)', note: null }
const idle = () => () => {}
const empty = () => EMPTY

const RING = 169.6 // 2π · 27
/** The four heights a way can sit at beside the painting, in half-sizes from its centre. */
const ROWS = [-0.8, -0.27, 0.27, 0.8]

function layoutFor(w: number, h: number) {
  const narrow = w <= 860
  const half = narrow ? Math.min(w * 0.36, h * 0.2) : Math.min(h * 0.33, w * 0.2)
  return { cx: w / 2, cy: narrow ? h * 0.27 : h * 0.47, half, narrow, w, h }
}

/**
 * Where Drift folds into: the rift seam on Raven's photo in the console, or the pill's seam
 * when the console is closed. Both are RiftSpectrum canvases — the listening crack — so the
 * screen goes back into the thing that has been hearing the music all along.
 */
function seamTarget(): HTMLElement | null {
  for (const sel of ['.console__seam', '.np__seam']) {
    const el = document.querySelector<HTMLElement>(sel)
    if (el && el.offsetWidth > 0) return el
  }
  return null
}

/** The crack the screen folds into, measured now: centre, untransformed width, lean. */
function foldTarget(el: HTMLElement | null): FoldTarget {
  if (!el) return { x: window.innerWidth / 2, y: window.innerHeight * 0.82, w: 180, rot: 0 }
  const r = el.getBoundingClientRect()
  let rot = 0
  const m = getComputedStyle(el).transform
  const v = m && m !== 'none' ? m.match(/matrix\(([^)]+)\)/)?.[1]?.split(',').map(Number) : undefined
  if (v && v.length >= 2) rot = Math.atan2(v[1]!, v[0]!)
  // Untransformed width: the seam leans, and its bounding box would overstate it.
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: el.offsetWidth, rot }
}

type Stage = 'open' | 'collapsing' | 'min' | 'expanding'

const fmt = (s: number): string => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

/** A copy of the chosen disc left where it was, draining away as its paint pours out. */
function drain(el: HTMLElement | null | undefined): void {
  const img = el?.querySelector('img')
  if (!el || !img) return
  const r = el.getBoundingClientRect()
  const g = document.createElement('span')
  g.className = 'drift-ghost'
  Object.assign(g.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` })
  g.append(img.cloneNode())
  document.body.append(g)
  window.setTimeout(() => g.remove(), 1000)
}

export default function Drift({ seed, from, minimized, onMinimize, onCover }: Props) {
  const root = useRef<HTMLDivElement>(null)
  const [stage, setStage] = useState<Stage>('open')
  const canvas = useRef<HTMLCanvasElement>(null)
  const [radio, setRadio] = useState<Radio | null>(null)
  const [size, setSize] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }))
  const view = useSyncExternalStore(radio?.subscribe ?? idle, radio?.getView ?? empty)
  const p = usePlayer()
  const bar = useRef<HTMLElement>(null)
  const clock = useRef<HTMLSpanElement>(null)
  const ring = useRef<SVGCircleElement>(null)
  const discs = useRef<Array<HTMLSpanElement | null>>([])
  const L = layoutFor(size.w, size.h)

  // One radio per mount. The parent keys this component by seed, so a new seed is a new radio.
  useEffect(() => {
    const c = canvas.current
    if (!c) return
    const r = new Radio(c)
    const l = layoutFor(window.innerWidth, window.innerHeight)
    r.layout(l.cx, l.cy, l.half)
    r.start(seed, r.pourFrom(from))
    setRadio(r)
    return () => r.dispose()
  }, [])

  useEffect(() => {
    radio?.layout(L.cx, L.cy, L.half)
  }, [radio, L.cx, L.cy, L.half])

  useEffect(() => {
    const onResize = () => {
      radio?.resize()
      setSize({ w: window.innerWidth, h: window.innerHeight })
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [radio])

  // Per-frame DOM: written straight to elements, never through state.
  useEffect(() => {
    if (!radio) return
    radio.locate = (i) => discs.current[i]?.getBoundingClientRect() ?? null
    let n = 0
    radio.onFrame = (cur, dur) => {
      if (bar.current) bar.current.style.transform = `scaleX(${(cur / dur).toFixed(4)})`
      // The ring fills toward the moment the mix begins, not the end of the clip.
      if (ring.current) ring.current.style.strokeDashoffset = String(RING * (1 - Math.min(1, cur / Math.max(1, dur - NEAR_END_S - 0.4))))
      if (++n % 15 === 0 && clock.current) {
        const v = radio.view
        const left = Math.max(0, dur - NEAR_END_S - cur)
        clock.current.textContent = `${fmt(cur)} / ${fmt(dur)}${v.ways[v.auto] && player.state.playing ? ` · drifting in ${fmt(left)}` : ''}`
      }
    }
  }, [radio])

  // Fold into the seam, or tear back out of it (lib/drift/fold.ts). The painting keeps
  // drawing through the fold and stops once it is gone; it starts again before the unfold,
  // so what comes out is live.
  useLayoutEffect(() => {
    const el = root.current
    if (!el) return
    let run: FoldRun | null = null
    let live = true
    if (minimized && (stage === 'open' || stage === 'expanding')) {
      const seam = seamTarget()
      setStage('collapsing')
      run = runFold(el, foldTarget(seam), 'in', () => impact(seam))
      void run.done.then(() => {
        if (!live) return
        setStage('min')
        if (radio) radio.hidden = true
      })
    } else if (!minimized && (stage === 'min' || stage === 'collapsing')) {
      if (radio) radio.hidden = false
      setStage('expanding')
      run = runFold(el, foldTarget(seamTarget()), 'out')
      void run.done.then(() => live && setStage('open'))
    }
    // An interrupted fold (restored mid-way) stops where it is; the next one takes over.
    return () => {
      live = false
      run?.cancel()
    }
    // Driven by `minimized` alone; `stage` is read, not watched, or each step would re-trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minimized, radio])

  useEffect(() => {
    onCover(stage === 'open')
  }, [stage, onCover])
  useEffect(() => () => onCover(false), [onCover])

  useEffect(() => {
    if (minimized) return
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || !radio) return
      if (e.key === 'Escape') onMinimize()
      else if (e.key === ' ') {
        e.preventDefault()
        player.toggle()
      } else if (e.key === 'ArrowRight' || e.key === 'n') take(radio.view.auto)
      else if (['1', '2', '3', '4'].includes(e.key)) take(Number(e.key) - 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [radio, onMinimize, minimized])

  function take(i: number): void {
    if (!radio?.view.ways[i]) return
    drain(discs.current[i])
    radio.go(i)
  }

  // Where the ways sit: redrawn once per set of ways, so they do not jump about on a resize.
  // Beside the painting's right edge, at shuffled heights, out from the edge by how unalike.
  const bearings = useMemo(() => {
    const rows = [...ROWS].sort(() => Math.random() - 0.5)
    return rows.map((row, i) => ({ row: row + (Math.random() - 0.5) * 0.14, gap: 0.14 + i * 0.15 + (Math.random() - 0.5) * 0.06 }))
  }, [view.ways])
  const spots = view.ways.map((_, i) => {
    const b = bearings[i]
    if (L.narrow || !b) return null
    return {
      x: Math.min(L.w - 290, L.cx + L.half * (1 + b.gap)),
      y: Math.max(96, Math.min(L.h - 150, L.cy + L.half * b.row)),
    }
  })

  const hop = view.path[view.at]
  const t = hop?.track ?? seed
  const prev = view.path[view.at - 1]?.track
  const why = hop?.score != null && prev ? `${Math.round(hop.score * 100)}% like ${prev.title.slice(0, 22)}` : 'drifting from here'

  return (
    <div
      ref={root}
      className={`drift is-${stage}`}
      style={{ '--drift-acc': view.accent } as React.CSSProperties}
      role="region"
      aria-label="Drift"
      aria-hidden={stage === 'min' || undefined}
    >
      <canvas ref={canvas} className="drift__paint" aria-hidden="true" />

      {!L.narrow && (
        <svg className="drift__wires" aria-hidden="true">
          {spots.map((s, i) => {
            if (!s) return null
            const dx = s.x - L.cx
            const dy = s.y - L.cy
            const len = Math.hypot(dx, dy) || 1
            const ux = dx / len
            const uy = dy / len
            // Leave the painting at its square edge; stop short of the disc.
            const edge = L.half / Math.max(Math.abs(ux), Math.abs(uy)) + 10
            return (
              <path
                key={i}
                className={i === view.auto ? 'is-auto' : undefined}
                d={`M${L.cx + ux * edge} ${L.cy + uy * edge} L${s.x - ux * 34} ${s.y - uy * 34}`}
              />
            )
          })}
        </svg>
      )}

      <header className="drift__top">
        <button type="button" className="ghost-btn drift__min" onClick={onMinimize} title="Back to the list — the music keeps playing (Esc)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="4 14 10 14 10 20" />
            <polyline points="20 10 14 10 14 4" />
            <line x1="14" y1="10" x2="21" y2="3" />
            <line x1="3" y1="21" x2="10" y2="14" />
          </svg>
          Back to the list
        </button>
        <p className="drift__mark">
          Drift<i />
        </p>
      </header>

      <aside className="drift__now">
        <p className="drift__kicker">
          <span>No. {String(Math.max(1, view.at + 1)).padStart(2, '0')}</span>
          <span className="drift__state">{!p.playing ? (p.buffering ? 'tuning…' : 'paused') : view.owed ? 'finding the way…' : 'on air'}</span>
        </p>
        <h2 className="drift__title" key={`t${t.track_id}`}>
          {t.title}
        </h2>
        <p className="drift__artist" key={`a${t.track_id}`}>
          {[t.artists ?? 'unknown artist', t.release_date?.slice(0, 4)].filter(Boolean).join(' · ')}
        </p>
        <div className="drift__bar">
          <i ref={bar} />
        </div>
        <p className="drift__time">
          <span ref={clock}>0:00</span>
          <span className="drift__why">{why}</span>
        </p>
        <div className="drift__actions">
          <button type="button" className="drift__btn" onClick={() => player.toggle()}>
            {p.playing ? 'Pause' : 'Play'}
          </button>
          <button type="button" className="drift__btn drift__btn--go" onClick={() => take(view.auto)} disabled={!view.ways.length}>
            Drift now →
          </button>
        </div>
      </aside>

      <section className={`drift__ways${L.narrow ? '' : ' is-scatter'}`} aria-label="Where to drift next">
        {view.ways.length === 0 && <p className="drift__wait">{view.note ?? 'listening for where this leads…'}</p>}
        {view.ways.map((w, i) => {
          const s = spots[i]
          const art = coverAt(w.track.cover_art_url, 64)
          return (
            <button
              type="button"
              key={w.track.track_id}
              className={`drift-way${i === view.auto ? ' is-auto' : ''}`}
              style={
                {
                  '--i': i,
                  transform: s ? `translate(${(s.x - 29).toFixed(1)}px, ${(s.y - 29).toFixed(1)}px)` : undefined,
                } as React.CSSProperties
              }
              onClick={() => take(i)}
            >
              <span className="drift-way__disc" ref={(el) => void (discs.current[i] = el)}>
                {art ? <img src={art} alt="" /> : <span className="drift-way__blank" />}
                <svg viewBox="0 0 58 58" aria-hidden="true">
                  <circle className="track" cx="29" cy="29" r="27" />
                  {i === view.auto && <circle ref={ring} className="fill" cx="29" cy="29" r="27" />}
                </svg>
                <b className="drift-way__key">{i + 1}</b>
              </span>
              <span className="drift-way__text">
                <em>
                  {i === view.auto ? 'next · ' : ''}
                  {w.label} · {Math.round(w.score * 100)}%
                </em>
                <strong>{w.track.title}</strong>
                <span>{w.track.artists ?? ''}</span>
              </span>
            </button>
          )
        })}
      </section>

      <nav className="drift__path" aria-label="Your drift so far">
        {view.path.map((h, i) => {
          const art = coverAt(h.track.cover_art_url, 64)
          return (
            <div key={`${i}-${h.track.track_id}`} className={`drift-hop${i === view.at ? ' is-now' : ''}`}>
              {i > 0 && <span className="drift-hop__gap">{h.score !== null ? Math.round(h.score * 100) : ''}</span>}
              <button
                type="button"
                title={`${h.track.title} — ${h.track.artists ?? ''}\nDrift again from here`}
                onClick={(e) => radio?.branch(i, e.currentTarget.getBoundingClientRect())}
              >
                {art && <img src={art} alt="" />}
              </button>
            </div>
          )
        })}
      </nav>

      <p className="drift__keys" aria-hidden="true">
        <kbd>space</kbd> pause <kbd>→</kbd> drift <kbd>1–4</kbd> steer <kbd>esc</kbd> back
      </p>
    </div>
  )
}
