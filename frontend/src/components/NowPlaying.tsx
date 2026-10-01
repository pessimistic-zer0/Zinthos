import { useRef } from 'react'
import type { TrackRecord } from '../lib/api'
import { coverAt, player, usePlayer } from '../lib/player'
import RiftSpectrum from './RiftSpectrum'
import '../styles/nowplaying.css'

interface Props {
  hue: number
  /** Start Drift from what is playing; handed the disc its paint pours out of. */
  onDrift: (t: TrackRecord, from: DOMRect) => void
}

/**
 * What is still playing once the console is closed. Music outlives the door you found it
 * through: close the console and the queue carries on, and this is where it can be paused,
 * skipped, stopped or drifted from. Only on the matrix — leaving through the rift stops it (see Choose).
 */
export default function NowPlaying({ hue, onDrift }: Props) {
  const s = usePlayer()
  const disc = useRef<HTMLSpanElement>(null)
  const t = s.track
  if (!t) return null
  const art = coverAt(t.cover_art_url, 64)

  return (
    <div className="np" style={{ '--hue': hue } as React.CSSProperties} role="region" aria-label="Now playing">
      <span className="np__disc" ref={disc} data-spin={s.playing || undefined}>
        {art ? <img src={art} alt="" draggable={false} /> : null}
      </span>
      <span className="np__text">
        <b>{t.title}</b>
        <span>{t.artists ?? 'unknown artist'}</span>
      </span>
      <RiftSpectrum hue={hue} points={26} className="np__seam" />
      <span className="np__controls">
        <button type="button" onClick={() => player.prev()} aria-label="Previous preview">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M6 5h2v14H6zM20 5.5v13L9.5 12z" /></svg>
        </button>
        <button type="button" className="np__main" onClick={() => player.toggle()} aria-label={s.playing ? 'Pause' : 'Play'}>
          {s.playing ? (
            <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" /></svg>
          ) : (
            <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M8 5.5v13l11-6.5z" /></svg>
          )}
        </button>
        <button type="button" onClick={() => player.next()} aria-label="Next preview">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M16 5h2v14h-2zM4 5.5v13L14.5 12z" /></svg>
        </button>
        <button
          type="button"
          className="np__drift"
          onClick={() => disc.current && onDrift(t, disc.current.getBoundingClientRect())}
          aria-label="Drift from here"
          title="Drift from here"
        >
          drift
        </button>
        <button type="button" onClick={() => player.stop()} aria-label="Stop">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
        </button>
      </span>
    </div>
  )
}
