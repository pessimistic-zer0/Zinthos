import { useCallback, useEffect, useRef, useState } from 'react'
import type { WarpOrigin } from '../App'
import { MODES, modeById, type Mode, type ModeKind } from '../lib/modes'
import QueryModal from '../components/QueryModal'
import ConstellationCanvas from '../components/ConstellationCanvas'
import ChooseBackdrop from '../components/ChooseBackdrop'
import '../styles/choose.css'

interface Props {
  /** True while the rift is still opening onto this screen. */
  holding: boolean
  /** The crack being opened; null when #/choose was opened directly. */
  origin: WarpOrigin | null
  onBack: () => void
}

export default function Choose({ holding, origin, onBack }: Props) {
  const [selected, setSelected] = useState<ModeKind | null>(null)
  const [open, setOpen] = useState(false)
  /** Where the console swoops in from: the clicked card's centre, relative to the viewport centre. */
  const [from, setFrom] = useState<{ x: number; y: number; tilt: number } | null>(null)
  // Captured once: whether this mount arrived through the warp. `holding` flips to false
  // on arrival, but the approach animation must stay on the element — and must NOT run at
  // all for someone who opened #/choose directly, who would otherwise get a slow zoom
  // with no flight in front of it.
  const [viaWarp] = useState(holding)
  const host = useRef<HTMLDivElement>(null)

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
    setOpen(true)
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Esc closes the console first, and only backs out to the landing page once there is
      // nothing left to close.
      if (open) close()
      else onBack()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, close, onBack])

  const active: Mode | null = selected ? modeById(selected) : null

  return (
    <div
      ref={host}
      className={`choose${viaWarp ? ' is-warp-arrival' : ''}${holding ? ' is-holding' : ''}`}
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
        <span className="hud-pill">
          <span className="pulse-dot" />
          Decision Matrix // Select 1 of 4
        </span>
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
          Four doors into the same 255 million tracks. Pick the one that matches what you
          already know.
        </p>
      </div>

      {MODES.map((mode, i) => (
        <div
          key={mode.id}
          className={`option option--${mode.corner}${selected === mode.id ? ' is-selected' : ''}`}
          style={
            {
              '--hue': mode.hue,
              '--stagger': `${i * 55}ms`,
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
            <b>Hover</b> Preview focus
          </span>
          <span>
            <b>Esc</b> Close / back
          </span>
        </div>
        <span className="choose__sig">Zinthos Matrix // 4-Way Selection</span>
      </footer>

      {active && open && <QueryModal mode={active} from={from} onClose={close} />}
    </div>
  )
}
