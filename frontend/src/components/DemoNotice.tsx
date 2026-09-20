import { useEffect, useState } from 'react'
import { formatCount, isDemo, onBootProgress, onIndexSize, type BootProgress } from '../lib/demo'
import '../styles/demo.css'

/**
 * A single line, fixed to the foot of the screen, saying what the hosted demo actually is.
 *
 * It exists because every other piece of copy in this app is written for the 255M-track
 * catalog, and the demo searches a slice of it. Rather than rewrite the voice of the whole
 * site for one deployment, one line states the difference and gives the real number.
 *
 * It does a second job before it does that one. A Space whose container was wiped re-pulls
 * the whole 27 GB slice before it can answer anything, and until this existed the only sign
 * of that was a query that sat on "querying the engine…" for four minutes. So while the
 * engine is warming, this same line reports the warm-up instead — with a real fraction, not
 * a spinner, because "6.2 of 27.0 GB" is the difference between waiting and wondering.
 *
 * Renders nothing unless the build set VITE_DEMO=1, so the local app is unchanged.
 * Dismissible, and the dismissal is remembered for the session only — a fresh visitor
 * always sees it.
 */

/** MB (10⁶ bytes, as the engine counts them) → GB, one decimal. */
const gb = (mb: number): string => (mb / 1000).toFixed(1)

function bootLine(b: BootProgress): string {
  if (b.stage === 'failed') return 'the engine failed to start — try again shortly.'
  if (b.stage === 'loading index') return 'catalogue loaded, opening the index.'
  if (b.stage === 'downloading') {
    if (b.downloadedMb === null) return 'loading the catalogue slice.'
    return b.totalMb
      ? `loading the catalogue slice — ${gb(b.downloadedMb)} of ${gb(b.totalMb)} GB.`
      : `loading the catalogue slice — ${gb(b.downloadedMb)} GB so far.`
  }
  // 'waking' (nothing is answering yet) and 'starting' (it is, but has not begun the pull).
  return 'starting the container.'
}

export default function DemoNotice() {
  const [count, setCount] = useState<number | null>(null)
  const [boot, setBoot] = useState<BootProgress | null>(null)
  const [hidden, setHidden] = useState(
    () => isDemo && sessionStorage.getItem('zinthos-demo-notice') === 'dismissed',
  )

  useEffect(() => onIndexSize(setCount), [])
  useEffect(() => onBootProgress(setBoot), [])

  if (!isDemo || hidden) return null

  // A bar only when there is a real denominator to divide by. Capped at 99% because the
  // download is not the whole boot — the index load follows it, and a bar that sits full
  // while the screen still says "wait" reads as a hang rather than as progress.
  const pct =
    boot && boot.stage === 'downloading' && boot.totalMb && boot.downloadedMb !== null
      ? Math.min(99, Math.round((boot.downloadedMb / boot.totalMb) * 100))
      : null

  return (
    <aside className={`demo-notice${boot ? ' demo-notice--boot' : ''}`} role="note">
      <span className="demo-notice__dot" aria-hidden="true" />
      <p className="demo-notice__text">
        {boot ? (
          <>
            <strong>Waking the engine.</strong> {bootLine(boot)}
          </>
        ) : count === null ? (
          <>
            <strong>Public demo.</strong> The full engine, running against a slice of the
            catalogue rather than all 255 million tracks.
          </>
        ) : (
          <>
            <strong>Public demo.</strong> {formatCount(count)} tracks — a slice of the 255
            million the pipeline was built on. Same engine, smaller index.
          </>
        )}
      </p>
      {pct !== null && (
        <span
          className="demo-notice__bar"
          role="progressbar"
          aria-label="Engine warm-up"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          {/* scaleX, not width: the repo's perf rules put transitions on transform and
              opacity only, and a bar is the one place it is tempting to forget. */}
          <i style={{ transform: `scaleX(${pct / 100})` }} />
        </span>
      )}
      <button
        type="button"
        className="demo-notice__close"
        aria-label="Dismiss demo notice"
        onClick={() => {
          sessionStorage.setItem('zinthos-demo-notice', 'dismissed')
          setHidden(true)
        }}
      >
        ×
      </button>
    </aside>
  )
}
