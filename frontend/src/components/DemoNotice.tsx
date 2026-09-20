import { useEffect, useState } from 'react'
import { formatCount, isDemo, onIndexSize } from '../lib/demo'
import '../styles/demo.css'

/**
 * A single line, fixed to the foot of the screen, saying what the hosted demo actually is.
 *
 * It exists because every other piece of copy in this app is written for the 255M-track
 * catalog, and the demo searches a slice of it. Rather than rewrite the voice of the whole
 * site for one deployment, one line states the difference and gives the real number.
 *
 * Renders nothing unless the build set VITE_DEMO=1, so the local app is unchanged.
 * Dismissible, and the dismissal is remembered for the session only — a fresh visitor
 * always sees it.
 */
export default function DemoNotice() {
  const [count, setCount] = useState<number | null>(null)
  const [hidden, setHidden] = useState(
    () => isDemo && sessionStorage.getItem('zinthos-demo-notice') === 'dismissed',
  )

  useEffect(() => onIndexSize(setCount), [])

  if (!isDemo || hidden) return null

  return (
    <aside className="demo-notice" role="note">
      <span className="demo-notice__dot" aria-hidden="true" />
      <p className="demo-notice__text">
        {count === null ? (
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
