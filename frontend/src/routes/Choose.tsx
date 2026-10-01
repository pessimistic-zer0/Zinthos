import { useCallback, useEffect, useRef, useState } from 'react'
import type { WarpOrigin } from '../App'
import { MODES, modeById, type Mode, type ModeKind } from '../lib/modes'
import QueryModal from '../components/QueryModal'
import ConstellationCanvas from '../components/ConstellationCanvas'
import ChooseBackdrop from '../components/ChooseBackdrop'
import NowPlaying from '../components/NowPlaying'
import { player, usePlayer } from '../lib/player'
import { api, type TrackRecord } from '../lib/api'
import { scene } from '../lib/scene'
import Drift from './Drift'
import '../styles/choose.css'
import { formatCount, isDemo, onIndexSize } from '../lib/demo'

interface Props {
  /** True while the rift is still opening onto this screen. */
  holding: boolean
  /** True while it is sealing again and this screen is being clipped away. */
  closing: boolean
  /** The crack being opened; null when #/choose was opened directly. */
  origin: WarpOrigin | null
  onBack: () => void
}

export default function Choose({ holding, closing, origin, onBack }: Props) {
  const [selected, setSelected] = useState<ModeKind | null>(null)
  const [open, setOpen] = useState(false)
  /** Where the console swoops in from: the clicked card's centre, relative to the viewport centre. */
  const [from, setFrom] = useState<{ x: number; y: number; tilt: number } | null>(null)
  /** Live track count from the engine, for the demo disclaimer. null until /health answers. */
  const [sliceSize, setSliceSize] = useState<number | null>(null)

  useEffect(() => onIndexSize(setSliceSize), [])
  // The music belongs to this side of the rift. Going back through it stops the queue.
  useEffect(() => () => player.stop(), [])
  /** The hue of the last door opened, which the now-playing pill keeps after it closes. */
  const [lastHue, setLastHue] = useState<number>(MODES[0]?.hue ?? 278)

  /**
   * Drift: the seed, where it was chosen on screen (its paint pours from there), and whether
   * it is folded away into the rift. Held here rather than in App so the console underneath
   * keeps its search, its trail and its scroll. `n` keys the Drift component, so drifting
   * from another song starts a fresh radio.
   *
   * Minimized, Drift is still mounted and still the radio — it keeps choosing and mixing —
   * only its screen is gone. It ends when the music is stopped.
   */
  const [drift, setDrift] = useState<{ seed: TrackRecord; from: DOMRect | null; n: number; min: boolean } | null>(null)
  const now = usePlayer()
  /** Whether the #/drift history entry is ours to go back from (not a pasted link). */
  const pushed = useRef(false)

  const showDrift = useCallback((id: number) => {
    if (!window.location.hash.startsWith('#/drift/')) {
      window.history.pushState(null, '', `#/drift/${id}`)
      pushed.current = true
    }
  }, [])

  /**
   * Play means Drift. A song already on air in a folded Drift unfolds it (and resumes it if
   * paused); any other song starts a new radio from it.
   */
  const openDrift = useCallback(
    (seed: TrackRecord, from: DOMRect | null) => {
      const onAir = player.state.track?.track_id === seed.track_id
      setDrift((d) => {
        if (d && onAir) return { ...d, min: false }
        return { seed, from, n: (d?.n ?? 0) + 1, min: false }
      })
      if (onAir && !player.state.playing) player.toggle()
      showDrift(seed.track_id)
    },
    [showDrift],
  )

  const minimizeDrift = useCallback(() => {
    // Leave the way we came in, so the back button does not have a dead step to walk through.
    if (pushed.current && window.location.hash.startsWith('#/drift/')) {
      pushed.current = false
      window.history.back()
    } else {
      window.history.replaceState(null, '', '#/choose')
      setDrift((d) => (d ? { ...d, min: true } : d))
    }
  }, [])

  useEffect(() => {
    const onHash = () => {
      const inDrift = window.location.hash.startsWith('#/drift/')
      // Back folds Drift away; forward unfolds it. Neither ends it.
      setDrift((d) => (d && d.min === inDrift ? { ...d, min: !inDrift } : d))
    }
    window.addEventListener('hashchange', onHash)
    // A pasted #/drift/{id}: fetch the seed and open on it, with no row to pour from.
    const m = window.location.hash.match(/^#\/drift\/(\d+)/)
    if (m) {
      api
        .track(Number(m[1]))
        .then((t) => (t.preview_url ? openDrift(t, null) : window.history.replaceState(null, '', '#/choose')))
        .catch(() => window.history.replaceState(null, '', '#/choose'))
    }
    return () => window.removeEventListener('hashchange', onHash)
  }, [openDrift])

  // Stopping the music ends Drift — the pill's stop is the way out of the radio.
  useEffect(() => {
    if (!now.track && drift?.min) setDrift(null)
  }, [now.track, drift?.min])

  const drifting = !!drift && !drift.min
  /** Drift is fully open over the matrix (never mid-fold): only then does the matrix go dark. */
  const [covered, setCovered] = useState(false)
  // Nothing of the matrix shows while Drift covers it: its starfield stops drawing.
  useEffect(() => {
    scene.covered = covered
    return () => {
      scene.covered = false
    }
  }, [covered])
  // Captured once: whether this mount arrived through the warp. `holding` flips to false
  // on arrival, but the approach animation must stay on the element — and must NOT run at
  // all for someone who opened #/choose directly, who would otherwise get a slow zoom
  // with no flight in front of it.
  const [viaWarp] = useState(holding)
  /**
   * The screen's own element, held in STATE behind a callback ref rather than in a useRef.
   *
   * ChooseBackdrop measures this box in a layout effect, and React commits bottom-up: a
   * child's layout effect runs BEFORE its parent's ref is attached. With a plain ref the
   * backdrop was handed `null` on the first commit, bailed out, and — its deps being two
   * stable values — was never told otherwise, so the tear and the wires simply never drew.
   *
   * It drew in development anyway, which is what hid this for so long: StrictMode re-runs
   * every effect on mount, and the second pass found the node. Production builds do not
   * double-invoke, so the deployed portal showed no rift at all while the dev server showed
   * one. State makes the node arrive as a render, which is a signal the child cannot miss.
   */
  const [host, setHost] = useState<HTMLDivElement | null>(null)

  /** Closing the console un-selects the card: a selection only lives while its console is open. */
  const close = useCallback(() => {
    setOpen(false)
    setSelected(null)
  }, [])

  const choose = useCallback((id: ModeKind, el: HTMLElement) => {
    const r = el.getBoundingClientRect()
    const tilt = parseFloat(getComputedStyle(el.parentElement ?? el).getPropertyValue('--tilt')) || 0
    setFrom({
      x: r.left + r.width / 2 - window.innerWidth / 2,
      y: r.top + r.height / 2 - window.innerHeight / 2,
      tilt,
    })
    setSelected(id)
    setLastHue(modeById(id).hue)
    setOpen(true)
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Drift is on top and handles its own Esc.
      if (e.key !== 'Escape' || drifting) return
      // Esc closes the console first, and only backs out to the landing page once there is
      // nothing left to close.
      if (open) close()
      else onBack()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, close, onBack, drifting])

  const active: Mode | null = selected ? modeById(selected) : null

  return (
    <>
    <div
      ref={setHost}
      className={`choose${viaWarp ? ' is-warp-arrival' : ''}${holding ? ' is-holding' : ''}${closing ? ' is-closing' : ''}${covered ? ' is-drifting' : ''}`}
      style={
        origin
          ? ({ '--warp-x': `${origin.x}px`, '--warp-y': `${origin.y}px` } as React.CSSProperties)
          : undefined
      }
    >
      <div className="choose__bg" aria-hidden="true">
        <img src="/assets/collage-bg.jpg" alt="" className="choose__collage" />
        <span className="choose__ambient" />
        {/* The same stars as the landing, at half strength: the other side is still the same sky. */}
        <ConstellationCanvas intensity={1.5} />
        <span className="grid-veil" />
      </div>
      {/* She came through with you: her silhouette, in the page's violet, behind the hub. */}
      <div className="choose__raven" aria-hidden="true" />
      <ChooseBackdrop origin={origin} host={host} />

      <header className="choose__nav">
        <div className="choose__nav-actions">
          <button type="button" className="ghost-btn" onClick={onBack}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="15 18 9 12 15 6" />
            </svg>
            Back to Portal
          </button>
        </div>
      </header>

      <div className="choose__hud">
        <span className="choose__tag">Interactive Selection</span>
        <h1 className="choose__title">Choose Your Way In</h1>
        <p className="choose__desc">
          {isDemo
            ? 'Four doors into the same catalogue. Pick the one that matches what you already know.'
            : 'Four doors into the same 255 million tracks. Pick the one that matches what you already know.'}
        </p>
        {isDemo && (
          <p className="choose__note">
            {sliceSize === null
              ? 'This live demo runs on a slice of the catalogue, not all 255 million tracks — obscure searches may come up short.'
              : `This live demo searches ${formatCount(sliceSize)} tracks — a slice of the 255 million the pipeline was built on. Obscure searches may come up short.`}
          </p>
        )}
      </div>

      {MODES.map((mode, i) => (
        <div
          key={mode.id}
          className={`option option--${mode.corner}${selected === mode.id ? ' is-selected' : ''}`}
          style={
            {
              '--hue': mode.hue,
              '--stagger': `${i * 55}ms`,
              '--focus-card': mode.focus.card,
            } as React.CSSProperties
          }
        >
          <button type="button" className="polaroid" onClick={(e) => choose(mode.id, e.currentTarget)}>
            <span className="polaroid__badge">
              <span className="polaroid__check">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </span>
              {mode.badge}
            </span>
            <span className="polaroid__photo">
              <img src={mode.card} alt={mode.persona} draggable={false} />
              <span className="polaroid__sheen" />
            </span>
            <span className="polaroid__footer">
              <span className="polaroid__title">
                {mode.name}
                <i>{mode.index}</i>
              </span>
              <span className="polaroid__tagline">{mode.tagline}</span>
              <span className="polaroid__chip">
                {selected === mode.id ? 'Selected ✓' : 'Click to Select'}
              </span>
            </span>
          </button>
        </div>
      ))}

      <footer className="choose__footer">
        <div className="choose__hints">
          <span>
            <b>Click</b> Select &amp; open console
          </span>
          <span>
            <b>Esc</b> Close / back
          </span>
        </div>
      </footer>

      {!open && <NowPlaying hue={lastHue} onDrift={openDrift} />}

      {active && open && <QueryModal mode={active} from={from} onClose={close} onDrift={openDrift} drifting={!!drift} />}
    </div>
    {drift && <Drift key={drift.n} seed={drift.seed} from={drift.from} minimized={drift.min} onMinimize={minimizeDrift} onCover={setCovered} />}
    </>
  )
}
