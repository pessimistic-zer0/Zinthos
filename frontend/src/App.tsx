import { useCallback, useEffect, useRef, useState } from 'react'
import Landing from './routes/Landing'
import Choose from './routes/Choose'
import About from './routes/About'
import RiftReveal from './components/RiftReveal'
import DemoNotice from './components/DemoNotice'
import type { RiftGeometry } from './lib/rift'
import { prefersReducedMotion, riftCloseMs, warpDurationMs } from './lib/warp'
import './styles/global.css'

/**
 * Both screens live in one document on purpose: the rift has to open uninterrupted onto the
 * card reveal, and a navigation mid-animation would tear it. The hash still changes so the
 * back button and a pasted #/choose link both behave.
 */
export type Phase = 'landing' | 'warp' | 'choose' | 'close'

/** Raven's on-screen box at the click, so the reveal can pick her up exactly where she was. */
export interface FigureSnapshot {
  x: number
  y: number
  w: number
  h: number
  src: string
}

/** The crack she guards (centre, length, lean in viewport px) plus where she stood. */
export interface WarpOrigin extends RiftGeometry {
  figure: FigureSnapshot | null
}

const phaseFromHash = (): Phase =>
  window.location.hash === '#/choose' ? 'choose' : 'landing'

export default function App() {
  const [phase, setPhase] = useState<Phase>(phaseFromHash)
  const [origin, setOrigin] = useState<WarpOrigin | null>(null)
  const timer = useRef<number | null>(null)

  // The back button (and a hand-edited hash) rewind the phase. A warp in flight is
  // abandoned rather than allowed to land on a screen the user just left.
  useEffect(() => {
    const onHash = () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current)
        timer.current = null
      }
      setPhase(phaseFromHash())
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  const enter = useCallback((from: WarpOrigin) => {
    if (timer.current !== null) return // already accelerating; ignore repeat clicks
    setOrigin(from)

    if (prefersReducedMotion()) {
      window.location.hash = '#/choose'
      setPhase('choose')
      return
    }

    setPhase('warp')
    timer.current = window.setTimeout(() => {
      timer.current = null
      // Replacing rather than pushing keeps a single back-step out of the matrix.
      window.location.hash = '#/choose'
      setPhase('choose')
    }, warpDurationMs())
  }, [])

  /**
   * Back out the way you came in: the same tear, sealing. Only the tear, though — the way
   * in also dollies the matrix in and drags the landing along, and replaying either
   * backwards would put a second of zoom between the click and the home screen. So the
   * landing simply mounts at rest behind the hole and the hole shrinks off it.
   *
   * Needs the geometry of the crack you arrived through. Without it — #/choose opened
   * directly, or reduced motion — there is nothing to close, so it is still a cut.
   */
  const exit = useCallback(() => {
    if (timer.current !== null) return // already sealing
    if (!origin || prefersReducedMotion()) {
      window.location.hash = ''
      setPhase('landing')
      return
    }

    setPhase('close')
    timer.current = window.setTimeout(() => {
      timer.current = null
      window.location.hash = ''
      setPhase('landing')
    }, riftCloseMs())
  }, [origin])

  return (
    <>
      {phase !== 'choose' && (
        <Landing phase={phase} origin={origin} onEnter={enter} />
      )}
      {/* The page under the landing. Only while she is on guard: the warp and the matrix
          are fixed screens and must not be scrolled out from under the rift. */}
      {phase === 'landing' && <About />}
      {phase !== 'landing' && (
        <Choose
          holding={phase === 'warp'}
          closing={phase === 'close'}
          origin={origin}
          onBack={exit}
        />
      )}
      {phase === 'warp' && origin && <RiftReveal origin={origin} />}
      {phase === 'close' && origin && <RiftReveal origin={origin} mode="close" />}
      {/* Hosted build only (VITE_DEMO=1); renders nothing locally. */}
      <DemoNotice />
    </>
  )
}
