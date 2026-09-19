import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { shieldPaths } from '../lib/shield'

/** How far past the frame box the SVG reaches, so shards can float outside the rim. */
const MARGIN = 16

/** Frosted grain: monochrome fractal noise at ~0.8% alpha — felt, not seen. Decoded once, tiled as a pattern. */
const GRAIN =
  "data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E" +
  "%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E" +
  "%3CfeColorMatrix values='0 0 0 0 1  0 0 0 0 0.95  0 0 0 0 1  0 0 0 0.008 0'/%3E%3C/filter%3E" +
  "%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E"

/** Pointy-top hexagon lattice, one tile: a full cell and the two halves that seam it. */
const HEX_R = 15
const HEX_W = Math.sqrt(3) * HEX_R
const HEX_H = 3 * HEX_R
function hex(cx: number, cy: number): string {
  const pts: string[] = []
  for (let k = 0; k < 6; k++) {
    const a = Math.PI / 2 + (k * Math.PI) / 3
    pts.push(`${(cx + HEX_R * Math.cos(a)).toFixed(2)} ${(cy - HEX_R * Math.sin(a)).toFixed(2)}`)
  }
  return `M${pts.join('L')}Z`
}
const HEX_TILE = hex(HEX_W / 2, HEX_R) + hex(0, 2.5 * HEX_R) + hex(HEX_W, 2.5 * HEX_R)

/**
 * Raven's shield: a pane of dark glass over the whole HUD frame, rim bitten and cracked from
 * holding the rift shut for too long. See `lib/shield.ts` for the damage.
 *
 * What makes it read as a barrier rather than a window: a hexagonal energy lattice under the
 * glass, faint in the middle, dense at the rim and lit around the impact sites; ripple rings
 * where the hardest blows landed; and (on RiftCanvas, because she moves) threads of energy
 * from the orbs in her hands out to the rim.
 *
 * Glass without blur. The pane is fills — sheen, grain, a glint band, a violet edge haze,
 * the masked lattice — and the rim is strokes: a wide faint glow under a thin bright line, brighter and
 * whiter on the raw edges inside each chip. Plain SVG paint, rasterised once; nothing here
 * runs per frame, which matters on a page whose canvases already draw every frame.
 */
export default function ShieldFrame() {
  const ref = useRef<SVGSVGElement>(null)
  const [size, setSize] = useState<[number, number]>([0, 0])

  useLayoutEffect(() => {
    const host = ref.current?.parentElement
    if (!host) return
    const ro = new ResizeObserver(([entry]) => {
      if (!entry) return
      const { width, height } = entry.contentRect
      const w = Math.round(width)
      const h = Math.round(height)
      setSize((s) => (s[0] === w && s[1] === h ? s : [w, h]))
    })
    ro.observe(host)
    return () => ro.disconnect()
  }, [])

  const [w, h] = size
  const paths = useMemo(() => (w > 0 && h > 0 ? shieldPaths(w, h, MARGIN) : null), [w, h])
  if (!paths) return <svg className="shield" ref={ref} aria-hidden="true" />

  const W = w + 2 * MARGIN
  const H = h + 2 * MARGIN
  return (
    <svg className="shield" ref={ref} viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true">
      <defs>
        <linearGradient id="shield-sheen" x1="0" y1="0" x2="1" y2="0.6">
          <stop offset="0" stopColor="#fff" stopOpacity="0.01" />
          <stop offset="0.25" stopColor="#fff" stopOpacity="0.004" />
          <stop offset="0.45" stopColor="#fff" stopOpacity="0" />
          <stop offset="0.65" stopColor="#fff" stopOpacity="0" />
          <stop offset="1" stopColor="#c4b5fd" stopOpacity="0.008" />
        </linearGradient>
        <linearGradient id="shield-glint" x1="0" y1="0" x2="1" y2="0.45">
          <stop offset="0.18" stopColor="#fff" stopOpacity="0" />
          <stop offset="0.3" stopColor="#fff" stopOpacity="0.007" />
          <stop offset="0.35" stopColor="#fff" stopOpacity="0.012" />
          <stop offset="0.41" stopColor="#fff" stopOpacity="0.005" />
          <stop offset="0.52" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <radialGradient id="shield-haze" cx="0.5" cy="0.5" r="0.72">
          <stop offset="0.5" stopColor="#a855f7" stopOpacity="0" />
          <stop offset="0.82" stopColor="#d8ccf0" stopOpacity="0.006" />
          <stop offset="1" stopColor="#d8ccf0" stopOpacity="0.018" />
        </radialGradient>
        <pattern id="shield-grain" patternUnits="userSpaceOnUse" width="180" height="180">
          <image href={GRAIN} width="180" height="180" />
        </pattern>
        <pattern id="shield-hex" patternUnits="userSpaceOnUse" width={HEX_W} height={HEX_H}>
          <path d={HEX_TILE} fill="none" stroke="rgba(216,180,254,0.17)" strokeWidth="0.8" />
        </pattern>
        {/* Luminance mask for the lattice: dark (hidden) in the middle, bright toward the rim,
            and a hot spot on every impact so the cells light up where the blows landed. */}
        <radialGradient id="shield-hex-rim" cx="0.5" cy="0.5" r="0.72">
          <stop offset="0.45" stopColor="#fff" stopOpacity="0" />
          <stop offset="0.85" stopColor="#fff" stopOpacity="0.28" />
          <stop offset="1" stopColor="#fff" stopOpacity="0.62" />
        </radialGradient>
        <radialGradient id="shield-hex-hit">
          <stop offset="0" stopColor="#fff" stopOpacity="1" />
          <stop offset="0.5" stopColor="#fff" stopOpacity="0.45" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
        <mask id="shield-hex-mask" maskUnits="userSpaceOnUse" x="0" y="0" width={W} height={H}>
          <rect width={W} height={H} fill="url(#shield-hex-rim)" />
          {paths.impacts.map((i, k) => (
            <circle key={k} cx={i.x} cy={i.y} r={i.r} fill="url(#shield-hex-hit)" />
          ))}
        </mask>
        <clipPath id="shield-clip">
          <path d={paths.pane} />
        </clipPath>
      </defs>

      {/* The pane */}
      <path d={paths.pane} fill="rgba(255,255,255,0.005)" />
      <path d={paths.pane} fill="url(#shield-sheen)" />
      <path d={paths.pane} fill="url(#shield-grain)" />
      <path d={paths.pane} fill="url(#shield-glint)" />
      <path d={paths.pane} fill="url(#shield-haze)" />
      {/* The lattice: the energy the glass is made of */}
      <path d={paths.pane} fill="url(#shield-hex)" mask="url(#shield-hex-mask)" />
      {/* Ripples where it was hit hardest */}
      <g clipPath="url(#shield-clip)" fill="none" stroke="#d8b4fe">
        {paths.impacts.map((i, k) => (
          <g key={k}>
            <circle cx={i.x} cy={i.y} r={i.r * 0.38} strokeOpacity="0.16" strokeWidth="1.2" />
            <circle cx={i.x} cy={i.y} r={i.r * 0.66} strokeOpacity="0.1" strokeWidth="1" />
            <circle cx={i.x} cy={i.y} r={i.r * 0.95} strokeOpacity="0.05" strokeWidth="1" />
          </g>
        ))}
      </g>

      {/* The rim: energy holding the edge */}
      <g fill="none" strokeLinecap="round" strokeLinejoin="round">
        <path d={paths.rim} stroke="rgba(168,85,247,0.08)" strokeWidth="8" />
        <path d={paths.rim} stroke="rgba(216,180,254,0.3)" strokeWidth="1.2" />
        {/* Raw edges: where it broke, the energy is bare and white-hot */}
        <path d={paths.raw} stroke="rgba(192,132,252,0.16)" strokeWidth="10" />
        <path d={paths.raw} stroke="rgba(233,213,255,0.3)" strokeWidth="3" />
        <path d={paths.raw} stroke="rgba(250,245,255,0.6)" strokeWidth="1.4" />
        {/* Fractures running in from the bites */}
        <path d={paths.cracks} stroke="rgba(216,180,254,0.07)" strokeWidth="2.5" />
        <path d={paths.cracks} stroke="rgba(240,230,255,0.2)" strokeWidth="0.9" />
        {/* Shards */}
        <path d={paths.shards} fill="rgba(216,180,254,0.08)" stroke="rgba(233,213,255,0.4)" strokeWidth="1" />
      </g>
    </svg>
  )
}
