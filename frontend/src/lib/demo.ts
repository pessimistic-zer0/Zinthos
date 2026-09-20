/**
 * Demo-slice awareness, and the cold boot behind it.
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
 *
 * ── ONE POLLER, TWO QUESTIONS ────────────────────────────────────────────────────────────
 * Two things wait on `/health`, and they used to be one: "how big is the index" (asked once,
 * answered once) and "how far along is the boot" (asked repeatedly, changes every second).
 * They share a poller rather than running two, because they are answered by the same
 * response and a second timer would only double the traffic against a Space that is already
 * busy pulling 27 GB.
 */
import { api, type HealthResponse } from './api'

export const isDemo = import.meta.env.VITE_DEMO === '1'

/** How far the hosted engine has got through its cold start. Null once it is serving. */
export interface BootProgress {
  /**
   * `waking` is ours — the container is not answering at all yet. The rest come from
   * demo/app.py's Boot: `starting`, `downloading`, `loading index`, `failed`.
   */
  stage: string
  /** Null until the download begins, or when the engine is past it. */
  downloadedMb: number | null
  /** Null whenever the Hub could not be asked for the slice's size. No denominator, no bar. */
  totalMb: number | null
  elapsedS: number
}

/** Tracks in the live index, once /health has answered. null until then (or if it never does). */
let vectors: number | null = null
const sizeWaiting = new Set<(n: number) => void>()
const bootWatching = new Set<(p: BootProgress | null) => void>()
let polling = false

/**
 * The hosted engine answers /health with 200 and status != "ok" while it is still loading —
 * a 503 there gets the Space killed by the platform's probe, so readiness lives in the body.
 * Treat anything but "ok" as "not yet" and keep waiting.
 */
const RETRY_MS = 4000

/**
 * Twelve minutes. The old ceiling was 60 tries — four minutes — which was SHORTER THAN THE
 * COLD START IT WAS WAITING FOR: a wiped container re-pulls the whole 27 GB slice, measured
 * at 235 s on 2026-09-21, and then loads the index on top of that. The poll could give up
 * while the engine was doing exactly what it had just said it was doing, and the screen
 * would settle into wording that made no numeric claim as though the engine were dead.
 */
const MAX_TRIES = 180

const emitBoot = (p: BootProgress | null): void => {
  bootWatching.forEach((fn) => fn(p))
}

const progressOf = (h: HealthResponse): BootProgress => ({
  stage: h.stage ?? 'starting',
  downloadedMb: h.downloaded_mb ?? null,
  totalMb: h.total_mb ?? null,
  elapsedS: h.elapsed_s ?? 0,
})

/** Nobody left to answer: stop the timer rather than poll an empty room. */
const watched = (): boolean => sizeWaiting.size > 0 || bootWatching.size > 0

function again(tries: number): void {
  if (tries < MAX_TRIES && watched()) {
    window.setTimeout(() => poll(tries + 1), RETRY_MS)
    return
  }
  // Out of patience. Drop the progress rather than freeze a stale percentage on screen —
  // callers fall back to wording that makes no numeric claim.
  polling = false
  emitBoot(null)
  sizeWaiting.clear()
}

function poll(tries = 0): void {
  if (!isDemo) return
  api
    .health()
    .then((h) => {
      if (h.status !== 'ok' || !h.vectors) {
        emitBoot(progressOf(h))
        again(tries)
        return
      }
      vectors = h.vectors
      sizeWaiting.forEach((fn) => fn(h.vectors))
      sizeWaiting.clear()
      emitBoot(null)
      polling = false
    })
    .catch(() => {
      // fetch rejected, so the engine is not listening yet — the platform is still building
      // the container and nothing inside it has bound the port. That is a real, reportable
      // stage of the wait, not an error to hide.
      emitBoot({ stage: 'waking', downloadedMb: null, totalMb: null, elapsedS: 0 })
      again(tries)
    })
}

function start(): void {
  if (polling || !isDemo) return
  polling = true
  poll()
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
  sizeWaiting.add(fn)
  start()
  return () => sizeWaiting.delete(fn)
}

/**
 * Subscribe to the cold-boot progress. Fires repeatedly while the engine warms and once with
 * null when it is ready (or when the poll gives up), so a caller can simply render whatever
 * it was last handed and disappear on null.
 *
 * Subscribing also STARTS the poll, and that matters for more than this readout: the first
 * `/health` is what wakes a sleeping Space. DemoNotice mounts on the landing screen, so the
 * wake now begins while the visitor is still reading the hero — not when they reach the
 * matrix — and the rift animation covers a wait that has already been running.
 */
export function onBootProgress(fn: (p: BootProgress | null) => void): () => void {
  if (!isDemo) return () => {}
  // Already serving: say so, rather than subscribing to a poll that has stopped. Silence
  // here would strand a caller that mounted while the engine was warming — it would hold
  // the last progress it was handed and go on showing it forever. Same contract as
  // onIndexSize: if the answer is known, deliver it now.
  if (vectors !== null) {
    fn(null)
    return () => {}
  }
  bootWatching.add(fn)
  start()
  return () => bootWatching.delete(fn)
}

export const formatCount = (n: number): string => n.toLocaleString('en-US')
