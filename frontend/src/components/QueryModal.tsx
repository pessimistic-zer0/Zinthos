import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  api,
  EngineError,
  type LibraryScanResponse,
  type TrackRecord,
} from '../lib/api'
import type { Mode } from '../lib/modes'
import { splitNameLine } from '../lib/nameline'
import { player, usePlayer } from '../lib/player'
import RiftSpectrum from './RiftSpectrum'
import SoundProfile, { type Heard } from './SoundProfile'
import TrackRow from './TrackRow'
import '../styles/modal.css'

type Status = 'idle' | 'loading' | 'done' | 'error'

/**
 * One screen of results. The console keeps a stack of them, because every result is now a
 * door of its own: "neighbours" on any row pushes that track's neighbourhood, and the trail
 * above the list walks back. A search replaces the stack; a hop adds to it.
 */
interface View {
  key: number
  /** Breadcrumb text. */
  label: string
  rows: TrackRecord[]
  /** The `similar` mode's first step: rows are catalog copies to pick from, not results. */
  pickable?: boolean
  seed?: TrackRecord
  heard?: Heard
  scan?: LibraryScanResponse
  /** A note that is not an error: "nothing matched", an unmatched sample, and so on. */
  note?: string | undefined
  /** Vibe search pages; the next offset to ask for, or absent when there is no more. */
  more?: { q: string; offset: number } | undefined
}

const PAGE = 30

/**
 * What she says about the track playing. Deterministic per track, so replaying one does not
 * change her mind — she is not a slot machine.
 */
const VERDICTS = [
  'Fine. This one stays.',
  "I didn't hate it. Don't tell anyone.",
  'Darker than I expected. Good.',
  'It hums like the rift does at night.',
  'Play it again. Quietly.',
  'Acceptable. Barely.',
  'That low end knows things.',
  'You can keep this one.',
  'Hm. Somebody meant this.',
  'Louder. No — exactly like that.',
]
const verdictFor = (t: TrackRecord): string => VERDICTS[t.track_id % VERDICTS.length] ?? VERDICTS[0]!

const clip = (s: string, n = 26): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

interface Props {
  mode: Mode
  /** Where to swoop in from, relative to the viewport centre; null rises from just below. */
  from: { x: number; y: number; tilt: number } | null
  onClose: () => void
  /** Start Drift from a result: the track, and where its cover is on screen. */
  onDrift: (t: TrackRecord, cover: DOMRect) => void
  /** A Drift is running (folded into the rift or not): the bar offers the way back into it. */
  drifting: boolean
}

export default function QueryModal({ mode, from, onClose, onDrift, drifting }: Props) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState('')
  const [views, setViews] = useState<View[]>([])
  const [paging, setPaging] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const keySeq = useRef(0)
  const nowPlaying = usePlayer()

  const view = views[views.length - 1] ?? null

  useEffect(() => {
    input.current?.focus()
  }, [mode.id])

  // A new view starts at its top, not wherever the last one was scrolled to.
  useEffect(() => {
    list.current?.scrollTo({ top: 0 })
  }, [view?.key])

  const fail = useCallback((e: unknown) => {
    setError(e instanceof EngineError ? e.message : 'something went wrong')
    setStatus('error')
  }, [])

  const make = (v: Omit<View, 'key'>): View => ({ ...v, key: ++keySeq.current })

  const run = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      const q = query.trim()
      if (!q || status === 'loading') return

      setStatus('loading')
      setError('')

      try {
        switch (mode.id) {
          case 'vibe': {
            const r = await api.search(q, PAGE)
            setViews([
              make({
                label: clip(q),
                rows: r.results,
                heard: r,
                // A parse that matched nothing is not an error — it means the phrasing fell
                // outside the rules and the LLM was not available to cover it.
                note:
                  r.results.length === 0 && r.llm_fallback_recommended
                    ? 'nothing matched — the rules missed this phrasing and no LLM is configured'
                    : undefined,
                more: r.results.length === PAGE ? { q, offset: PAGE } : undefined,
              }),
            ])
            break
          }
          case 'similar': {
            const { title, artist } = splitNameLine(q)
            const r = await api.byName(title, artist, 10)
            if (r.error) {
              setError(r.error)
              setStatus('error')
              return
            }
            setViews([make({ label: `“${clip(title, 20)}”`, rows: r.candidates, pickable: true })])
            break
          }
          case 'playlist': {
            const r = await api.playlist(q, PAGE)
            setViews([make({ label: clip(q), rows: r.tracks, heard: { source: r.source, filters: r.filters } })])
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
              return
            }
            const r = await api.libraryScan(tracks, PAGE)
            setViews([make({ label: `your ${r.total} tracks`, rows: r.recommendations, scan: r })])
            break
          }
        }
        setStatus('done')
      } catch (err) {
        fail(err)
      }
    },
    [mode.id, query, status, fail],
  )

  /**
   * A track's neighbourhood, pushed on top of wherever you are. Step 2 of `similar` is this
   * same move — the chosen copy becomes the embedding seed — so the picker replaces its own
   * view rather than leaving "pick a copy" in the trail as a dead step.
   */
  const wander = useCallback(
    async (t: TrackRecord, replace = false) => {
      if (status === 'loading') return
      setStatus('loading')
      setError('')
      try {
        const r = await api.similar(t.track_id, PAGE)
        const next = make({ label: `≈ ${clip(t.title, 22)}`, rows: r.results, seed: t })
        setViews((vs) => (replace ? [...vs.slice(0, -1), next] : [...vs, next]))
        setStatus('done')
      } catch (err) {
        fail(err)
      }
    },
    [status, fail],
  )

  const dig = useCallback(async () => {
    const v = view
    if (!v?.more || paging) return
    setPaging(true)
    try {
      const r = await api.search(v.more.q, PAGE, v.more.offset)
      const seen = new Set(v.rows.map((t) => t.track_id))
      const fresh = r.results.filter((t) => !seen.has(t.track_id))
      setViews((vs) =>
        vs.map((x) =>
          x.key === v.key
            ? {
                ...x,
                rows: [...x.rows, ...fresh],
                more: r.results.length === PAGE ? { q: v.more!.q, offset: v.more!.offset + PAGE } : undefined,
              }
            : x,
        ),
      )
    } catch (err) {
      fail(err)
    } finally {
      setPaging(false)
    }
  }, [view, paging, fail])

  /** Up/down walk the rows; the input hands focus down into the list and takes it back. */
  const onListKey = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const rows = Array.from(list.current?.querySelectorAll<HTMLButtonElement>('[data-row]') ?? [])
    const at = rows.indexOf(document.activeElement as HTMLButtonElement)
    if (at < 0) return
    e.preventDefault()
    const next = rows[at + (e.key === 'ArrowDown' ? 1 : -1)]
    if (next) next.focus()
    else if (e.key === 'ArrowUp') input.current?.focus()
  }, [])

  /**
   * Play is Drift: a song pressed here opens it, pouring out of that row's cover (or unfolds
   * a Drift already playing it). The list stays underneath for when Drift is folded away.
   */
  const driftFrom = useCallback(
    (t: TrackRecord) => {
      if (!t.preview_url) return
      const cover = list.current?.querySelector<HTMLElement>(`[data-track="${t.track_id}"] .row__cover`)
      const seam = document.querySelector<HTMLElement>('.console__seam')
      onDrift(t, (cover ?? seam)?.getBoundingClientRect() ?? new DOMRect(window.innerWidth / 2, window.innerHeight / 2, 46, 46))
    },
    [onDrift],
  )

  const multiline = mode.id === 'library'
  const rows = view?.rows ?? []
  const playing = nowPlaying.track
  const legend = view?.pickable ? `Pick a copy (${rows.length})` : `Results (${rows.length})`

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
        className={`console${nowPlaying.playing ? ' is-sounding' : ''}`}
        style={
          {
            '--hue': mode.hue,
            '--focus-full': mode.focus.full,
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
            {/* The rift, listening. A seam across her frame that opens with the music. */}
            <RiftSpectrum hue={mode.hue} className="console__seam" />
            {playing && (
              <span className="console__now" key={playing.track_id}>
                <b>{playing.title}</b>
                <span>{playing.artists ?? 'unknown artist'}</span>
              </span>
            )}
          </div>
          <div className="console__speech" key={playing?.track_id ?? 'idle'}>
            {playing ? `“${verdictFor(playing)}”` : mode.speech}
          </div>
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
                if (e.key === 'ArrowDown' && !multiline) {
                  const first = list.current?.querySelector<HTMLButtonElement>('[data-row]')
                  if (first) {
                    e.preventDefault()
                    first.focus()
                  }
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

            {views.length > 1 && (
              <nav className="trail" aria-label="Where you have wandered">
                {views.map((v, i) => (
                  <button
                    type="button"
                    key={v.key}
                    className={`trail__step${i === views.length - 1 ? ' is-here' : ''}`}
                    disabled={i === views.length - 1 || status === 'loading'}
                    onClick={() => setViews((vs) => vs.slice(0, i + 1))}
                  >
                    {v.label}
                  </button>
                ))}
              </nav>
            )}

            <div className="results" ref={list} onKeyDown={onListKey}>
              {status === 'loading' && (
                <div className="skeleton" aria-label="querying the engine">
                  {Array.from({ length: 7 }, (_, i) => (
                    <span key={i} style={{ '--i': i } as React.CSSProperties} />
                  ))}
                </div>
              )}

              {status === 'error' && <p className="results__note results__note--bad">{error}</p>}

              {status === 'idle' && views.length === 0 && (
                <p className="results__note">awaiting input.</p>
              )}

              {status !== 'loading' && view && (
                <>
                  {view.heard && <SoundProfile heard={view.heard} key={view.key} />}

                  {view.note && <p className="results__note results__note--bad">{view.note}</p>}

                  {view.seed && (
                    <p className="results__note">
                      neighbours of <b>{view.seed.title}</b> — {view.seed.artists ?? 'unknown artist'}
                    </p>
                  )}

                  {view.scan && <LibraryBreakdown scan={view.scan} />}

                  {view.pickable && rows.length > 0 && (
                    <p className="results__note">which one did you mean? each copy has its own neighbours.</p>
                  )}

                  {!view.note && rows.length === 0 && <p className="results__note">no matches.</p>}

                  {rows.length > 0 && !view.pickable && (
                    <div className="results__bar">
                      <button
                        type="button"
                        className="results__play"
                        onClick={() => {
                          if (playing) player.toggle()
                          else {
                            const t = rows.find((x) => x.preview_url)
                            if (t) driftFrom(t)
                          }
                        }}
                      >
                        {playing && nowPlaying.playing ? (
                          <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" /></svg>
                        ) : (
                          <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M8 5.5v13l11-6.5z" /></svg>
                        )}
                        {playing ? (nowPlaying.playing ? 'Pause' : 'Resume') : mode.id === 'playlist' && !view.seed ? 'Play the sequence' : 'Play'}
                      </button>
                      {drifting && playing && (
                        <button
                          type="button"
                          className="results__drift"
                          onClick={() => driftFrom(playing)}
                          title="Unfold Drift out of the rift"
                        >
                          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                            <path d="M3 9c3-3 6 3 9 0s6-3 9 0M3 15c3-3 6 3 9 0s6-3 9 0" />
                          </svg>
                          Back to Drift
                        </button>
                      )}
                      {playing && (
                        <button type="button" className="results__skip" onClick={() => player.next()} aria-label="Next preview">
                          next ›
                        </button>
                      )}
                      <span className="results__count">
                        {rows.filter((t) => t.preview_url).length} of {rows.length} have a preview
                      </span>
                    </div>
                  )}

                  {rows.map((t, i) => {
                    const current = playing?.track_id === t.track_id
                    return (
                      <TrackRow
                        key={t.track_id}
                        t={t}
                        i={i}
                        prefix={view.pickable ? 'OPT' : 'REC'}
                        current={current}
                        playing={current && nowPlaying.playing}
                        buffering={current && nowPlaying.buffering}
                        onPreview={() => (current && nowPlaying.playing ? player.toggle() : driftFrom(t))}
                        onMain={() => (view.pickable ? void wander(t, true) : driftFrom(t))}
                        onWander={view.seed?.track_id === t.track_id ? undefined : () => void wander(t)}
                        onDrift={view.pickable ? undefined : (r) => onDrift(t, r)}
                      />
                    )
                  })}

                  {view.more && (
                    <button type="button" className="results__more" onClick={() => void dig()} disabled={paging}>
                      {paging ? 'digging…' : `Dig deeper — the next ${PAGE}`}
                    </button>
                  )}
                </>
              )}

              {nowPlaying.error && <p className="results__note results__note--bad">{nowPlaying.error}</p>}
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

/**
 * Mode 04's second answer. The scan returns what the shelf is made of — genres and decades —
 * alongside the recommendations, and until now only the recommendations were drawn.
 */
function LibraryBreakdown({ scan }: { scan: LibraryScanResponse }) {
  const genres = scan.breakdown.genres.slice(0, 6)
  const eras = [...scan.breakdown.eras].sort((a, b) => a.decade.localeCompare(b.decade))
  const gMax = Math.max(1, ...genres.map((g) => g.count))
  const eMax = Math.max(1, ...eras.map((e) => e.count))
  return (
    <div className="shelf">
      <p className="results__note">
        matched <b>{scan.matched}</b> of {scan.total}
        {scan.methods.isrc > 0 && <> — {scan.methods.isrc} by ISRC (exact)</>}
        {scan.methods.fuzzy > 0 && <> — {scan.methods.fuzzy} by name</>}
        {scan.unmatched > 0 && <> — {scan.unmatched} not found</>}
      </p>
      {(genres.length > 0 || eras.length > 0) && (
        <div className="shelf__grid">
          {genres.length > 0 && (
            <div className="shelf__col">
              <span className="shelf__head">Your shelf, by genre</span>
              {genres.map((g, i) => (
                <div key={g.genre} className="shelf__bar" style={{ '--v': g.count / gMax, '--i': i } as React.CSSProperties}>
                  <span>{g.genre}</span>
                  <i />
                  <b>{g.count}</b>
                </div>
              ))}
            </div>
          )}
          {eras.length > 0 && (
            <div className="shelf__col">
              <span className="shelf__head">By decade</span>
              <div className="shelf__eras">
                {eras.map((e, i) => (
                  <span key={e.decade} className="shelf__era" style={{ '--v': e.count / eMax, '--i': i } as React.CSSProperties} title={`${e.decade}: ${e.count}`}>
                    <i />
                    <em>{e.decade.replace(/^(\d\d)(\d0)s?$/, "'$2")}</em>
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      {scan.unmatched_samples.length > 0 && (
        <p className="results__note shelf__missed">
          not in the catalogue: {scan.unmatched_samples.slice(0, 3).map((s) => `${s.title}${s.artist ? ` — ${s.artist}` : ''}`).join(' · ')}
        </p>
      )}
    </div>
  )
}
