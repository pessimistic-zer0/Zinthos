/**
 * Demo-slice awareness.
 *
 * The hosted demo runs the SAME engine against a slice of the catalog — a few million of
 * the 255M tracks — because the full `master.db` is 162 GB and the FAISS index 9 GB, and
 * neither fits a free host. The copy in this app is written for the full catalog, so
 * shipping it unchanged onto the demo would claim a catalog the demo does not have.
 *
 * `VITE_DEMO=1` at build time turns this on. The local build is untouched: with the flag
 * unset, `isDemo` is false, nothing here fetches, and every screen reads exactly as before.
 *
 * The number itself comes from the engine (`/health` → `vectors`, the count actually in the
 * index), not from a constant, so it cannot drift away from what is really searchable.
 */
import { api } from './api'

export const isDemo = import.meta.env.VITE_DEMO === '1'

/** Tracks in the live index, once /health has answered. null until then (or if it never does). */
let vectors: number | null = null
const waiting = new Set<(n: number) => void>()
let asked = false

/**
 * The hosted engine answers /health with 503 while it is still loading its index — a cold
 * Space can spend a minute or two downloading the slice before it can serve anything. So a
 * failed probe is retried rather than treated as final, with a ceiling so a genuinely dead
 * engine does not poll forever.
 */
const RETRY_MS = 4000
const MAX_TRIES = 60

function ask(tries = 0): void {
  if ((asked && tries === 0) || !isDemo) return
  asked = true
  api
    .health()
    .then((h) => {
      // The hosted engine answers /health with 200 and status != "ok" while it is still
      // loading — a 503 there gets the Space killed by the platform's probe, so readiness
      // lives in the body. Treat anything but "ok" as "not yet" and keep waiting.
      if (h.status !== 'ok' || !h.vectors) {
        if (tries < MAX_TRIES && waiting.size > 0) {
          window.setTimeout(() => ask(tries + 1), RETRY_MS)
        }
        return
      }
      vectors = h.vectors
      waiting.forEach((fn) => fn(h.vectors))
      waiting.clear()
    })
    .catch(() => {
      // Still warming (or genuinely down): leave the count unknown rather than inventing
      // one — callers fall back to wording that makes no numeric claim — and look again.
      if (tries < MAX_TRIES && waiting.size > 0) {
        window.setTimeout(() => ask(tries + 1), RETRY_MS)
      } else {
        waiting.clear()
      }
    })
}

/**
 * Subscribe to the live index size. Calls back at most once, immediately if already known.
 * Returns an unsubscribe for the unmount-before-answer case.
 */
export function onIndexSize(fn: (n: number) => void): () => void {
  if (!isDemo) return () => {}
  if (vectors !== null) {
    fn(vectors)
    return () => {}
  }
  waiting.add(fn)
  ask()
  return () => waiting.delete(fn)
}

export const formatCount = (n: number): string => n.toLocaleString('en-US')
