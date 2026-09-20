import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  api,
  EngineError,
  type LibraryScanResponse,
  type TrackRecord,
} from '../lib/api'
import type { Mode } from '../lib/modes'
import { splitNameLine } from '../lib/nameline'
import '../styles/modal.css'

type Status = 'idle' | 'loading' | 'done' | 'error'

const fmtDuration = (ms: number | null): string => {
  if (!ms || ms <= 0) return '—'
  const total = Math.round(ms / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

/** The right-hand metadata strip. Falls back gracefully — the catalog is sparse in places. */
function trackMeta(t: TrackRecord): string {
  const bits: string[] = []
  if (typeof t.score === 'number') bits.push(`MATCH ${(t.score * 100).toFixed(1)}%`)
  else if (typeof t.popularity === 'number') bits.push(`POP ${t.popularity}`)
  if (t.release_date) bits.push(t.release_date.slice(0, 4))
  if (t.duration_ms) bits.push(fmtDuration(t.duration_ms))
  if (t.album_title) bits.push(t.album_title)
  return bits.join(' · ')
}

interface Props {
  mode: Mode
  /** Where to swoop in from, relative to the viewport centre; null rises from just below. */
  from: { x: number; y: number; tilt: number } | null
  onClose: () => void
}

export default function QueryModal({ mode, from, onClose }: Props) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState('')
  const [results, setResults] = useState<TrackRecord[]>([])
  /** Only the `similar` mode uses a second step: resolve the name, then pick a copy. */
  const [candidates, setCandidates] = useState<TrackRecord[] | null>(null)
  const [seed, setSeed] = useState<TrackRecord | null>(null)
  /** Mode 04 only: how the pasted lines resolved, so an ISRC visibly earns its keep. */
  const [scan, setScan] = useState<LibraryScanResponse | null>(null)
  const input = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    input.current?.focus()
  }, [mode.id])

  const fail = useCallback((e: unknown) => {
    setError(e instanceof EngineError ? e.message : 'something went wrong')
    setStatus('error')
  }, [])

  const run = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      const q = query.trim()
      if (!q || status === 'loading') return

      setStatus('loading')
      setError('')
      setCandidates(null)
      setSeed(null)
      setScan(null)
      setResults([])

      try {
        switch (mode.id) {
          case 'vibe': {
            const r = await api.search(q, 30)
            setResults(r.results)
            // A parse that matched nothing is not an error — it means the phrasing fell
            // outside the rules and the LLM was not available to cover it.
            if (r.results.length === 0 && r.llm_fallback_recommended) {
              setError('nothing matched — the rules missed this phrasing and no LLM is configured')
            }
            setStatus('done')
            break
          }
          case 'similar': {
            const { title, artist } = splitNameLine(q)
            const r = await api.byName(title, artist, 10)
            if (r.error) {
              setError(r.error)
              setStatus('error')
              break
            }
            setCandidates(r.candidates)
            setStatus('done')
            break
          }
          case 'playlist': {
            const r = await api.playlist(q, 30)
            setResults(r.tracks)
            setStatus('done')
            break
          }
          case 'library': {
            const tracks = q
              .split('\n')
              .map((l) => l.trim())
              .filter(Boolean)
              .map(splitNameLine)
            if (tracks.length === 0) {
              setStatus('idle')
              break
            }
            const r = await api.libraryScan(tracks, 30)
            setScan(r)
            setResults(r.recommendations)
            setStatus('done')
            break
          }
        }
      } catch (err) {
        fail(err)
      }
    },
    [mode.id, query, status, fail],
  )

  /** Step 2 of `similar`: the chosen catalog copy becomes the embedding seed. */
  const pick = useCallback(
    async (candidate: TrackRecord) => {
      setStatus('loading')
      setError('')
      setSeed(candidate)
      setCandidates(null)
      try {
        const r = await api.similar(candidate.track_id, 30)
        setResults(r.results)
        setStatus('done')
      } catch (err) {
        fail(err)
      }
    },
    [fail],
  )

  const multiline = mode.id === 'library'
  const rows = candidates ?? results
  const legend = candidates
    ? `Pick a copy (${candidates.length})`
    : `Results (${results.length})`

  return (
    <div
      className="backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={`${mode.name} console`}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className="console"
        style={
          {
            '--hue': mode.hue,
            '--from-x': `${(from?.x ?? 0).toFixed(0)}px`,
            '--from-y': `${(from?.y ?? 40).toFixed(0)}px`,
            '--from-tilt': `${from?.tilt ?? 0}deg`,
            '--from-scale': from ? 0.22 : 0.96,
          } as React.CSSProperties
        }
      >
        <button type="button" className="console__close" onClick={onClose} aria-label="Close (Esc)">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>

        <aside className="console__persona">
          <div className="console__photo">
            <img src={mode.full} alt={mode.persona} draggable={false} />
            <span className="console__photo-badge">{mode.badge}</span>
          </div>
          <div className="console__speech">{mode.speech}</div>
          <div className="console__meta">
            <h2>{mode.name}</h2>
            <p>{mode.tagline}</p>
          </div>
          <ul className="console__can" aria-label="What this can do">
            {mode.abilities.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
          <p className="console__sig">Zinthos understands more than keywords.</p>
        </aside>

        <section className="console__work">
          <form className="field" onSubmit={run}>
            <span className="field__legend">Query</span>
            <textarea
              ref={input}
              className={`field__input${multiline ? ' field__input--tall' : ''}`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                // Enter submits; Shift+Enter is a newline, which only matters for the
                // library paste box but costs nothing to support everywhere.
                if (e.key === 'Enter' && !e.shiftKey && !multiline) {
                  e.preventDefault()
                  void run(e)
                }
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault()
                  void run(e)
                }
              }}
              placeholder={mode.placeholder}
              spellCheck={false}
              rows={multiline ? 5 : 1}
            />
            <div className="field__foot">
              <span className="field__hint">{mode.hint}</span>
              <button type="submit" className="field__go" disabled={status === 'loading'}>
                {status === 'loading' ? 'Working…' : 'Run'}
              </button>
            </div>
          </form>

          <div className="field field--results">
            <span className="field__legend">{legend}</span>
            <div className="results">
              {status === 'loading' && <p className="results__note">querying the engine…</p>}

              {status === 'error' && <p className="results__note results__note--bad">{error}</p>}

              {status === 'idle' && (
                <p className="results__note">
                  {seed ? `seeded from "${seed.title}"` : 'awaiting input.'}
                </p>
              )}

              {status === 'done' && error && (
                <p className="results__note results__note--bad">{error}</p>
              )}

              {status === 'done' && seed && (
                <p className="results__note">
                  neighbours of <b>{seed.title}</b> — {seed.artists ?? 'unknown artist'}
                </p>
              )}

              {status === 'done' && scan && (
                <p className="results__note">
                  matched <b>{scan.matched}</b> of {scan.total}
                  {scan.methods.isrc > 0 && <> — {scan.methods.isrc} by ISRC (exact)</>}
                  {scan.methods.fuzzy > 0 && <> — {scan.methods.fuzzy} by name</>}
                  {scan.unmatched > 0 && <> — {scan.unmatched} not found</>}
                </p>
              )}

              {status === 'done' && !error && rows.length === 0 && (
                <p className="results__note">no matches.</p>
              )}

              {status !== 'loading' &&
                rows.map((t, i) => {
                  const row = (
                    <>
                      <span className="row__prefix">
                        &gt; [{candidates ? 'OPT' : 'REC'}-{String(i + 1).padStart(2, '0')}]
                      </span>
                      <span className="row__body">
                        <span className="row__title">{t.title}</span>
                        <span className="row__desc">{t.artists ?? 'unknown artist'}</span>
                        <span className="row__meta">{trackMeta(t)}</span>
                      </span>
                    </>
                  )
                  return candidates ? (
                    <button
                      type="button"
                      key={t.track_id}
                      className="row row--pick"
                      onClick={() => void pick(t)}
                    >
                      {row}
                    </button>
                  ) : (
                    <div key={t.track_id} className="row">
                      {row}
                    </div>
                  )
                })}
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
