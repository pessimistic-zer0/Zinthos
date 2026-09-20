/**
 * Typed client for the Zinthos Engine (backend/engine/app.py).
 *
 * Every call goes through /api, which the Vite dev server proxies to the engine
 * (see vite.config.ts). That keeps the browser same-origin, so the engine's CORS
 * allowlist is a safety net rather than a dependency.
 */

const BASE = import.meta.env.VITE_ENGINE_BASE ?? '/api'

/** One display record, as `hydrate.hydrate()` builds it. */
export interface TrackRecord {
  track_id: number
  title: string
  artists: string | null
  album_title: string | null
  cover_art_url: string | null
  release_date: string | null
  preview_url: string | null
  duration_ms: number | null
  popularity: number | null
  is_explicit: number | null
  isrc: string | null
  /** Present only on /search/similar results. */
  score?: number
}

export interface SearchResponse {
  query: string
  source: 'rules' | 'rules+llm'
  coverage: number
  matched: string[]
  unmatched: string[]
  filters: unknown[]
  offset: number
  count: number
  results: TrackRecord[]
  llm_fallback_recommended?: boolean
}

export interface PlaylistResponse {
  query: string
  size: number
  source: string
  filters: unknown[]
  count: number
  tracks: TrackRecord[]
}

export interface ByNameResponse {
  title: string
  artist: string
  count: number
  candidates: TrackRecord[]
  need_artist?: boolean
  error?: string
}

export interface SimilarResponse {
  seed: number
  count: number
  results: TrackRecord[]
  norm: string
  filter: string
  gate: string
  pool: number
}

/**
 * Mode 04. The engine's field is `recommendations`, not `tracks` — see
 * `library.scan()` in backend/engine/library.py. It was typed as `tracks` here and read
 * as `r.tracks` in QueryModal, so the mode rendered an empty list.
 */
export interface LibraryScanResponse {
  total: number
  matched: number
  unmatched: number
  methods: { isrc: number; fuzzy: number }
  unmatched_samples: Array<{ title: string; artist: string }>
  breakdown: {
    genres: Array<{ genre: string; count: number }>
    eras: Array<{ decade: string; count: number }>
  }
  recommendations: TrackRecord[]
}

export interface HealthResponse {
  status: string
  vectors: number
  nprobe: number
  /**
   * Cold-boot fields. Present ONLY on the hosted demo and only while it is still warming:
   * demo/app.py answers /health with 200 throughout the boot (a 503 there gets the Space
   * killed by the platform's own probe), so readiness lives in `status`, and these describe
   * how far along it is. A local engine never sends them, which is why every one is optional.
   */
  stage?: string
  ready?: boolean
  elapsed_s?: number
  downloaded_mb?: number
  /** Absent whenever the Hub could not be asked for the slice's size — never assume 0. */
  total_mb?: number
  detail?: string
}

export class EngineError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'EngineError'
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    // fetch only rejects on a transport failure — the engine is not listening.
    throw new EngineError('engine unreachable — is the Zinthos engine running?', 0)
  }
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b: { detail?: string }) => b.detail)
      .catch(() => undefined)
    throw new EngineError(detail ?? `engine returned ${res.status}`, res.status)
  }
  return (await res.json()) as T
}

const qs = (params: Record<string, string | number>): string =>
  new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)]),
  ).toString()

export const api = {
  health: (): Promise<HealthResponse> => call<HealthResponse>('/health'),

  /** Mode 01 — natural-language vibe search. */
  search: (q: string, limit = 30, offset = 0): Promise<SearchResponse> =>
    call<SearchResponse>(`/search?${qs({ q, limit, offset })}`),

  /** Mode 02, step 1 — resolve a typed song name to catalog candidates. */
  byName: (title: string, artist = '', limit = 10): Promise<ByNameResponse> =>
    call<ByNameResponse>(`/search/by-name?${qs({ title, artist, limit })}`),

  /** Mode 02, step 2 — neighbours of a chosen candidate. */
  similar: (trackId: number, k = 30): Promise<SimilarResponse> =>
    call<SimilarResponse>(`/search/similar/${trackId}?${qs({ k })}`),

  /** Mode 03 — a sequenced playlist for a prompt. */
  playlist: (q: string, size = 30): Promise<PlaylistResponse> =>
    call<PlaylistResponse>('/playlist', {
      method: 'POST',
      body: JSON.stringify({ q, size }),
    }),

  /** Mode 04 — recommendations seeded by the listener's own files. */
  libraryScan: (
    tracks: Array<Pick<TrackRecord, 'title'> & Partial<TrackRecord>>,
    size = 30,
  ): Promise<LibraryScanResponse> =>
    call<LibraryScanResponse>('/library/scan', {
      method: 'POST',
      body: JSON.stringify({ tracks, size }),
    }),
}
