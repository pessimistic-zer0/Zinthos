import { useEffect, useRef } from 'react'
import type { TrackRecord } from '../lib/api'
import { coverAt, player } from '../lib/player'

const fmtDuration = (ms: number | null): string => {
  if (!ms || ms <= 0) return '—'
  const total = Math.round(ms / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

/** The metadata strip. Falls back gracefully — the catalog is sparse in places. */
function trackMeta(t: TrackRecord): string {
  const bits: string[] = []
  if (typeof t.score === 'number') bits.push(`MATCH ${(t.score * 100).toFixed(1)}%`)
  else if (typeof t.popularity === 'number') bits.push(`POP ${t.popularity}`)
  if (t.release_date) bits.push(t.release_date.slice(0, 4))
  if (t.duration_ms) bits.push(fmtDuration(t.duration_ms))
  if (t.album_title) bits.push(t.album_title)
  return bits.join(' · ')
}

/**
 * How far through the clip, as a hairline under the row. Written straight to the element's
 * transform every frame — a setState here would re-render the row at 60fps for one number.
 */
function Progress() {
  const bar = useRef<HTMLElement>(null)
  useEffect(() => {
    let raf = 0
    const tick = () => {
      if (bar.current) bar.current.style.transform = `scaleX(${player.progress().toFixed(4)})`
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])
  return <i ref={bar} className="row__progress" aria-hidden="true" />
}

interface Props {
  t: TrackRecord
  i: number
  /** OPT while picking a copy, REC for results. */
  prefix: 'OPT' | 'REC'
  current: boolean
  playing: boolean
  buffering: boolean
  /** The row's main action: play it, or (while picking a copy) choose it as the seed. */
  onMain: () => void
  onPreview: () => void
  /** Walk to this track's neighbours. Absent where it would be circular (the seed itself). */
  onWander?: (() => void) | undefined
  /** Start Drift from this track; handed the row's cover, which its paint pours out of. */
  onDrift?: ((cover: DOMRect) => void) | undefined
}

export default function TrackRow({ t, i, prefix, current, playing, buffering, onMain, onPreview, onWander, onDrift }: Props) {
  const cover = useRef<HTMLButtonElement>(null)
  const thumb = coverAt(t.cover_art_url, 64)
  const hasPreview = !!t.preview_url
  const q = encodeURIComponent(`${t.title} ${t.artists ?? ''}`.trim())

  return (
    <div
      data-track={t.track_id}
      className={`row${prefix === 'OPT' ? ' row--pick' : ''}${current ? ' is-current' : ''}${playing ? ' is-playing' : ''}${hasPreview ? '' : ' is-silent'}`}
      style={{ '--i': Math.min(i, 14) } as React.CSSProperties}
    >
      <span className="row__prefix" aria-hidden="true">
        [{prefix}-{String(i + 1).padStart(2, '0')}]
      </span>

      <button
        type="button"
        ref={cover}
        className="row__cover"
        onClick={onPreview}
        disabled={!hasPreview}
        aria-label={hasPreview ? `${current && playing ? 'Pause' : 'Play'} preview of ${t.title}` : 'No preview in the catalogue'}
        title={hasPreview ? undefined : 'No preview in the catalogue'}
      >
        {thumb ? <img src={thumb} alt="" loading="lazy" decoding="async" draggable={false} /> : <span className="row__cover-blank" />}
        <span className="row__glyph" aria-hidden="true">
          {current && playing ? (
            <span className="eq">
              <i />
              <i />
              <i />
            </span>
          ) : current && buffering ? (
            <span className="row__spin" />
          ) : hasPreview ? (
            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
              <path d="M8 5.5v13l11-6.5z" />
            </svg>
          ) : null}
        </span>
      </button>

      <button type="button" className="row__main" onClick={onMain} data-row>
        <span className="row__title">{t.title}</span>
        <span className="row__desc">{t.artists ?? 'unknown artist'}</span>
        <span className="row__meta">
          {trackMeta(t)}
          {!hasPreview && <em> · no preview</em>}
        </span>
      </button>

      <span className="row__actions">
        {onWander && (
          <button type="button" className="row__act" onClick={onWander} title="Neighbours of this track" aria-label={`Tracks that sound like ${t.title}`}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="6" cy="12" r="2.5" />
              <circle cx="18" cy="5.5" r="2" />
              <circle cx="18" cy="18.5" r="2" />
              <path d="M8.3 10.8 16 6.6M8.3 13.2 16 17.4" />
            </svg>
          </button>
        )}
        {onDrift && hasPreview && (
          <button
            type="button"
            className="row__act row__act--drift"
            onClick={() => cover.current && onDrift(cover.current.getBoundingClientRect())}
            title="Drift from here — an endless radio of its neighbours"
            aria-label={`Drift from ${t.title}`}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M3 9c3-3 6 3 9 0s6-3 9 0M3 15c3-3 6 3 9 0s6-3 9 0" />
            </svg>
            <span className="row__act-label">Drift</span>
          </button>
        )}
        <a
          className="row__act"
          href={`https://open.spotify.com/search/${q}`}
          target="_blank"
          rel="noreferrer"
          title="Find the full track on Spotify"
          aria-label={`Find ${t.title} on Spotify`}
        >
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 17 17 7M9 7h8v8" />
          </svg>
        </a>
      </span>

      {current && <Progress />}
    </div>
  )
}
