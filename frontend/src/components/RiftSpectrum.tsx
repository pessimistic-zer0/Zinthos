import { useEffect, useRef } from 'react'
import { player, usePlayer } from '../lib/player'

/**
 * The live spectrum, drawn as the rift.
 *
 * At silence it is the seam from the landing: a hairline crack with a lit edge. With sound
 * it opens — each point along it is one band of the spectrum, low on the left, and how far
 * the lips part there is how loud that band is. The ends are pinned shut, so it always reads
 * as a tear in something rather than as a bar chart.
 *
 * Same rules as RiftCanvas: no shadowBlur, glow is a wide low-alpha stroke under a thin
 * bright one. The loop only runs while there is something to move — playing, or still
 * closing after a pause — and parks on one final hairline frame otherwise.
 */

interface Props {
  hue: number
  /** Points along the seam. More reads finer; 24 is plenty at pill size. */
  points?: number
  className?: string
}

/** Deterministic zigzag, so a crack keeps its shape between frames and between mounts. */
function jitter(n: number, seed: number): Float32Array {
  const out = new Float32Array(n)
  let s = seed
  for (let i = 0; i < n; i++) {
    s = (s * 16807) % 2147483647
    out[i] = (s / 2147483647) * 2 - 1
  }
  return out
}

export default function RiftSpectrum({ hue, points = 44, className }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  /** Survives the effect re-running on play/pause, so a pause lets the tear close rather than snapping it shut. */
  const openRef = useRef<Float32Array | null>(null)
  const { playing } = usePlayer()

  useEffect(() => {
    const c = canvas.current
    const g = c?.getContext('2d')
    if (!c || !g) return

    const n = points
    const jitA = jitter(n, 7919)
    const jitB = jitter(n, 104729)
    const jitX = jitter(n, 1299709)
    /** Smoothed opening per point: fast attack, slow release, like a VU needle. */
    if (openRef.current?.length !== n) openRef.current = new Float32Array(n)
    const open = openRef.current
    let w = 0
    let h = 0
    let raf = 0

    const size = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = c.clientWidth
      h = c.clientHeight
      c.width = Math.max(1, Math.round(w * dpr))
      c.height = Math.max(1, Math.round(h * dpr))
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    size()
    const ro = new ResizeObserver(() => {
      size()
      draw()
    })
    ro.observe(c)

    /**
     * Point i reads a log-spaced slice of the 256 bins: the bottom few bins are where
     * almost all the energy is, and a linear map would spend half the seam on the
     * near-silent top octave.
     */
    const bandOf = (i: number): [number, number] => {
      const a = Math.floor(Math.pow(128, i / n)) - 1
      const b = Math.max(a + 1, Math.floor(Math.pow(128, (i + 1) / n)))
      return [Math.max(1, a), b]
    }

    const edge = (sign: 1 | -1, mid: number) => {
      g.beginPath()
      for (let i = 0; i < n; i++) {
        const t = i / (n - 1)
        const x = t * w + (i > 0 && i < n - 1 ? (jitX[i] ?? 0) * (w / n) * 0.3 : 0)
        const j = sign < 0 ? (jitA[i] ?? 0) : (jitB[i] ?? 0)
        // The hairline's own crookedness, present even at silence.
        const crook = j * h * 0.035
        const y = mid + crook + sign * (open[i] ?? 0) * (0.75 + 0.25 * j)
        if (i === 0) g.moveTo(x, y)
        else g.lineTo(x, y)
      }
    }

    function draw() {
      g!.clearRect(0, 0, w, h)
      const mid = h / 2

      // The hole: everything between the lips.
      g!.beginPath()
      edge(-1, mid)
      for (let i = n - 1; i >= 0; i--) {
        const t = i / (n - 1)
        const x = t * w + (i > 0 && i < n - 1 ? (jitX[i] ?? 0) * (w / n) * 0.3 : 0)
        const y = mid + (jitB[i] ?? 0) * h * 0.035 + (open[i] ?? 0) * (0.75 + 0.25 * (jitB[i] ?? 0))
        g!.lineTo(x, y)
      }
      g!.closePath()
      g!.fillStyle = 'rgba(3, 1, 8, 0.92)'
      g!.fill()

      for (const sign of [-1, 1] as const) {
        edge(sign, mid)
        g!.lineJoin = 'round'
        g!.strokeStyle = `hsla(${hue}, 90%, 62%, 0.16)`
        g!.lineWidth = 7
        g!.stroke()
        g!.strokeStyle = `hsla(${hue}, 95%, 72%, 0.45)`
        g!.lineWidth = 2.5
        g!.stroke()
        g!.strokeStyle = `hsla(${hue}, 100%, 92%, 0.95)`
        g!.lineWidth = 0.9
        g!.stroke()
      }
    }

    const frame = () => {
      const bins = player.spectrum()
      const live = player.state.playing
      let moving = false
      for (let i = 0; i < n; i++) {
        let target = 0
        if (live && bins) {
          const [a, b] = bandOf(i)
          let s = 0
          for (let k = a; k < b; k++) s += bins[k] ?? 0
          const v = s / ((b - a) * 255)
          // Pinned at both ends: a tear, not an equaliser.
          const t = i / (n - 1)
          const env = Math.pow(Math.sin(Math.PI * t), 0.7)
          target = Math.pow(v, 1.5) * env * h * 0.46
        }
        const cur = open[i] ?? 0
        const next = cur + (target - cur) * (target > cur ? 0.55 : 0.12)
        open[i] = next
        if (next > 0.15) moving = true
      }
      draw()
      raf = live || moving ? requestAnimationFrame(frame) : 0
    }

    raf = requestAnimationFrame(frame)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [hue, points, playing])

  return <canvas ref={canvas} className={className} aria-hidden="true" />
}
