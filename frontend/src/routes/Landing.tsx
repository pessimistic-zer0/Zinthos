import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Phase, WarpOrigin } from '../App'
import ConstellationCanvas from '../components/ConstellationCanvas'
import RiftCanvas from '../components/RiftCanvas'
import ShieldFrame from '../components/ShieldFrame'
import { layoutBoxIn, riftFromLayout, type RiftGeometry } from '../lib/rift'
import { trackCursor } from '../lib/cursor'
import { useRavenEvasion } from '../lib/useRavenEvasion'
import { scene } from '../lib/scene'
import '../styles/landing.css'

interface Props {
  phase: Phase
  origin: WarpOrigin | null
  onEnter: (from: WarpOrigin) => void
}

export default function Landing({ phase, origin, onEnter }: Props) {
  const figure = useRef<HTMLImageElement>(null)
  const wrapper = useRef<HTMLDivElement>(null)
  const shadow = useRef<HTMLDivElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  /** The crack as the idle canvas last measured it — the launch must open THAT crack. */
  const rift = useRef<RiftGeometry | null>(null)
  const onGeometry = useCallback((g: RiftGeometry) => {
    rift.current = g
  }, [])

  const [covered, setCovered] = useState(false)

  const refs = useMemo(() => ({ figure, wrapper, shadow }), [])
  useRavenEvasion(refs, phase === 'landing')
  useEffect(() => trackCursor(), [])

  // Once the about panel has scrolled over the whole viewport, nothing of this screen shows.
  // Hide it and let the canvases skip their drawing until it is back.
  useEffect(() => {
    const onScroll = () => {
      const c = window.scrollY >= window.innerHeight - 2
      if (c !== scene.covered) {
        scene.covered = c
        setCovered(c)
      }
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      window.removeEventListener('scroll', onScroll)
      scene.covered = false
    }
  }, [])

  const scrollToAbout = useCallback(() => {
    document.getElementById('about')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  // The T is set in em relative to her rendered height, which only layout knows.
  useEffect(() => {
    const f = figure.current
    const w = wrapper.current
    if (!f || !w) return
    const ro = new ResizeObserver(() => w.style.setProperty('--raven-h', `${f.offsetHeight}px`))
    ro.observe(f)
    return () => ro.disconnect()
  }, [])

  /**
   * Open the crack she is guarding. The geometry comes from the idle canvas so the tear
   * that swallows the screen is pixel-for-pixel the seam that was glowing behind her.
   */
  const launch = useCallback(() => {
    const f = figure.current
    // Her live box, transforms included: the float, the evasion drift and the hover scale
    // all move her, and the reveal must pick her up from there or she jumps on the click.
    const r = f?.getBoundingClientRect()
    const snapshot =
      f && r ? { x: r.left, y: r.top, w: r.width, h: r.height, src: f.currentSrc || f.src } : null
    const s = stage.current?.getBoundingClientRect()
    const g =
      rift.current ??
      (s
        ? riftFromLayout(s, f && stage.current ? layoutBoxIn(f, stage.current, s) : null)
        : { x: window.innerWidth / 2, y: window.innerHeight / 2, len: 400, tilt: -0.21 })
    // Each letter gets its own vector to the seam; the pull keyframes read it back.
    stage.current?.querySelectorAll<HTMLElement>('.wordmark__letter').forEach((el) => {
      const b = el.getBoundingClientRect()
      el.style.setProperty('--to-x', `${(g.x - (b.left + b.width / 2)).toFixed(1)}px`)
      el.style.setProperty('--to-y', `${(g.y - (b.top + b.height / 2)).toFixed(1)}px`)
    })
    onEnter({ ...g, figure: snapshot })
  }, [onEnter])

  const warping = phase === 'warp'
  const style = origin
    ? ({
        '--warp-x': `${origin.x}px`,
        '--warp-y': `${origin.y}px`,
      } as React.CSSProperties)
    : undefined

  return (
    <div className={`landing${warping ? ' is-warping' : ''}${covered ? ' is-covered' : ''}`} style={style}>
      <div className="landing__glow" aria-hidden="true">
        <span className="landing__glow-wide" />
        <span className="landing__glow-core" />
        <span className="vignette" />
      </div>

      <ConstellationCanvas />
      <RiftCanvas stage={stage} figure={figure} onGeometry={onGeometry} />

      <div className="hud-frame">
        <ShieldFrame />
        <header className="landing__top">
          <a
            className="ghost-btn landing__gh"
            href="https://github.com/pessimistic-zer0/Zinthos"
            target="_blank"
            rel="noreferrer"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
            </svg>
            <span className="landing__gh-text">GitHub</span>
          </a>
        </header>
        <div className="stage" ref={stage}>
          <div className="wordmark" aria-hidden="true">
            {/* Letter by letter: the ones nearest the rift lean into it, and the outer ones
                have taken hits — chips out of the glyphs, cut with static clip-paths in
                percentages of the letter box so they scale with the type. */}
            <h1 className="wordmark__half wordmark__half--left" data-text="ZIN">
              <span className="wordmark__letter wordmark__letter--z" data-chip="0.9,0.16">Z</span>
              <span className="wordmark__letter">I</span>
              <span className="wordmark__letter wordmark__letter--n" data-chip="0.44,0.14">N</span>
            </h1>
            <h1 className="wordmark__half wordmark__half--right" data-text="HOS">
              <span className="wordmark__letter wordmark__letter--h">H</span>
              <span className="wordmark__letter wordmark__letter--o" data-chip="0.96,0.52">O</span>
              <span className="wordmark__letter wordmark__letter--s" data-chip="0.16,0.9">S</span>
            </h1>
          </div>
          <h1 className="sr-only">Zinthos</h1>

          <div className="raven" ref={wrapper}>
            {/* The missing letter. ZIN and HOS spell the name without its T; her pose is
                the T. A hollow one sits behind her, crossbar on her arms, stem leaning
                with the crack, so the name is only whole once you read her as a letter. */}
            <span className="raven__t" aria-hidden="true">
              T
            </span>
            <button
              type="button"
              className="raven__button"
              onClick={launch}
              aria-label="Enter the rift"
            >
              <img
                ref={figure}
                className="raven__figure"
                src="/assets/raven-hero.png"
                alt="Raven, floating with outstretched arms, guarding a crack of light behind her"
                draggable={false}
              />
            </button>
            <div className="raven__shadow" ref={shadow} aria-hidden="true" />
          </div>

          <div className="pitch">
            <p className="pitch__body">Find music by how it feels.</p>
            <button type="button" className="cta" onClick={launch}>
              <span>Enter</span>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
          </div>

          <div className="tagline" aria-hidden="true">
            <p>A different way to listen.</p>
            <p>Same music.</p>
            <p>Different dimensions.</p>
          </div>
        </div>

        <footer className="landing__footer">
          <button type="button" className="landing__footer-left" onClick={scrollToAbout}>
            ↓ Scroll to explore
          </button>
          <span className="landing__footer-center">She who guards the rift</span>
          <span className="landing__footer-right">
            Music <b>×</b> AI <b>×</b> Discovery <b>×</b> You
          </span>
        </footer>
      </div>
    </div>
  )
}
