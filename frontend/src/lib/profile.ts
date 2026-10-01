/**
 * What the engine heard, in words and ranges rather than SQL.
 *
 * A search comes back with its filters as `[column, op, value]` triples — the same
 * predicates `filters.build_where` bound into the WHERE clause (see backend/engine/rules.py
 * for the scales: audio features 0-1000, tempo in BPM, loudness in dB, genre_id 0-19).
 * Several triples usually land on one column (`energy > 300` AND `energy < 600`), and the
 * only honest picture of that is the interval they leave open. So each column collapses
 * to one [lo, hi] on its own domain, which the console draws as a lit stretch of a rail.
 */

export type Filter = [column: string, op: string, value: number]

export interface Rail {
  column: string
  label: string
  /** What the two ends of the rail mean, in the listener's words. */
  ends: [string, string]
  /** The open interval as fractions of the rail, 0-1. */
  lo: number
  hi: number
  /** The same interval in the column's own units, for the readout. */
  text: string
  /** The filters asked for an empty interval (lo > hi). The engine's merge guards this; we still say so. */
  empty: boolean
}

interface Domain {
  label: string
  min: number
  max: number
  ends: [string, string]
  fmt: (v: number) => string
}

const unit = (v: number): string => (v / 1000).toFixed(2).replace(/^0/, '')

/** Order is the order the rails are drawn in: feel first, then the physical measures. */
const DOMAINS: Record<string, Domain> = {
  valence: { label: 'Mood', min: 0, max: 1000, ends: ['dark', 'bright'], fmt: unit },
  energy: { label: 'Energy', min: 0, max: 1000, ends: ['calm', 'intense'], fmt: unit },
  danceability: { label: 'Groove', min: 0, max: 1000, ends: ['still', 'danceable'], fmt: unit },
  acousticness: { label: 'Acoustic', min: 0, max: 1000, ends: ['electric', 'acoustic'], fmt: unit },
  instrumentalness: { label: 'Instrumental', min: 0, max: 1000, ends: ['vocal', 'no vocal'], fmt: unit },
  speechiness: { label: 'Spoken', min: 0, max: 1000, ends: ['sung', 'spoken'], fmt: unit },
  liveness: { label: 'Live', min: 0, max: 1000, ends: ['studio', 'on stage'], fmt: unit },
  tempo: { label: 'Tempo', min: 50, max: 200, ends: ['50', '200 bpm'], fmt: (v) => `${v}` },
  loudness: { label: 'Loudness', min: -30, max: 0, ends: ['-30', '0 dB'], fmt: (v) => `${v}` },
  popularity: { label: 'Fame', min: 0, max: 100, ends: ['deep cut', 'hit'], fmt: (v) => `${v}` },
  release_year: { label: 'Era', min: 1950, max: 2026, ends: ['1950', 'now'], fmt: (v) => `${v}` },
}

/** Mirrors MACRO_GENRES in backend/engine/library.py — genre_id is an index into it. */
export const GENRES = [
  'african', 'alternative', 'asian', 'christian', 'classical', 'country', 'dance',
  'electronic', 'folk', 'hip-hop', 'jazz', 'kids', 'latin', 'metal', 'other',
  'pop', 'r&b', 'reggae', 'rock', 'soundtrack',
] as const

const clamp01 = (v: number): number => Math.min(1, Math.max(0, v))

function isFilter(f: unknown): f is Filter {
  return (
    Array.isArray(f) &&
    f.length === 3 &&
    typeof f[0] === 'string' &&
    typeof f[1] === 'string' &&
    typeof f[2] === 'number'
  )
}

export interface Profile {
  rails: Rail[]
  genres: string[]
}

export function profileOf(filters: unknown[]): Profile {
  const bounds = new Map<string, { lo: number; hi: number }>()
  const genres: string[] = []

  for (const f of filters) {
    if (!isFilter(f)) continue
    const [col, op, v] = f
    if (col === 'genre_id') {
      const g = GENRES[v]
      if (op === '=' && g && !genres.includes(g)) genres.push(g)
      continue
    }
    const d = DOMAINS[col]
    if (!d) continue
    const b = bounds.get(col) ?? { lo: d.min, hi: d.max }
    if (op === '>' || op === '>=') b.lo = Math.max(b.lo, v)
    else if (op === '<' || op === '<=') b.hi = Math.min(b.hi, v)
    else if (op === '=') {
      b.lo = v
      b.hi = v
    }
    bounds.set(col, b)
  }

  const rails: Rail[] = []
  for (const [col, d] of Object.entries(DOMAINS)) {
    const b = bounds.get(col)
    if (!b) continue
    const span = d.max - d.min
    const loOpen = b.lo <= d.min
    const hiOpen = b.hi >= d.max
    const text = loOpen ? `< ${d.fmt(b.hi)}` : hiOpen ? `> ${d.fmt(b.lo)}` : `${d.fmt(b.lo)}–${d.fmt(b.hi)}`
    rails.push({
      column: col,
      label: d.label,
      ends: d.ends,
      lo: clamp01((b.lo - d.min) / span),
      hi: clamp01((b.hi - d.min) / span),
      text,
      empty: b.lo > b.hi,
    })
  }
  return { rails, genres }
}
