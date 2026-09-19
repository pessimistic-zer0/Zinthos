import { useLayoutEffect, useState, type RefObject } from 'react'
import type { WarpOrigin } from '../App'
import { MODES } from '../lib/modes'
import { polygonToClip, reachOf, riftPolygon } from '../lib/rift'

interface Wire {
  corner: string
  hue: number
  /** Card centre (layout box, transforms ignored) → point on the hub's edge. */
  x1: number
  y1: number
  x2: number
  y2: number
}

interface Layout {
  w: number
  h: number
  tear: string
  wires: Wire[]
}

interface Props {
  origin: WarpOrigin | null
  host: RefObject<HTMLDivElement | null>
}

/** Where the segment from `from` toward `to` first meets the rectangle around `to`. */
function edgeOf(from: [number, number], to: [number, number], rect: DOMRect): [number, number] {
  const dx = to[0] - from[0]
  const dy = to[1] - from[1]
  let t = 1
  if (dx !== 0) {
    const tx = ((dx > 0 ? rect.left : rect.right) - from[0]) / dx
    if (tx > 0 && tx < t) t = tx
  }
  if (dy !== 0) {
    const ty = ((dy > 0 ? rect.top : rect.bottom) - from[1]) / dy
    if (ty > 0 && ty < t) t = ty
  }
  return [from[0] + dx * t, from[1] + dy * t]
}

/**
 * Two things behind the options, drawn once as SVG.
 *
 * The tear: the rift's outline at the size it had when it swallowed the screen, its rim lit
 * and everything past it a shade darker. You are inside the hole now. It is the same
 * polygon the landing draws, from the same origin, so it is the tear you came through.
 *
 * The wires: a line from each card to the centre hub, meeting the hub's box at a node.
 * A card's wire lights when you hover it and stays lit once it is selected (see the
 * `:has()` rules in choose.css). The matrix, literally.
 *
 * Positions come from layout boxes, not bounding rects: the cards fly in through a scale
 * animation and the hub is centred by a transform, and neither should move the wires.
 */
export default function ChooseBackdrop({ origin, host }: Props) {
  const [layout, setLayout] = useState<Layout | null>(null)

  useLayoutEffect(() => {
    const el = host.current
    if (!el) return

    const measure = () => {
      const w = el.clientWidth
      const h = el.clientHeight
      if (w === 0 || h === 0) return

      // ── The tear ───────────────────────────────────────────────────────────────────
      const g = origin ?? { x: w / 2, y: h / 2, len: h * 0.8, tilt: -0.21 }
      const reach = reachOf(g, w, h)
      // Open until the lens is nine tenths of the screen wide at its middle: its long sides
      // run just inside the viewport and its points leave through the top and bottom.
      const k = (0.46 * w) / reach
      const tear = polygonToClip(riftPolygon(g, k, reach)).replace(/px/g, '').slice('polygon('.length, -1)

      // ── The wires ──────────────────────────────────────────────────────────────────
      const hud = el.querySelector<HTMLElement>('.choose__hud')
      const wires: Wire[] = []
      if (hud && getComputedStyle(hud).position === 'absolute') {
        // The hub is centred with translate(-50%, -50%); its layout box's top-left is the
        // viewport centre, so shift by half its size to get the box you see.
        const hw = hud.offsetWidth
        const hh = hud.offsetHeight
        const hubRect = new DOMRect(hud.offsetLeft - hw / 2, hud.offsetTop - hh / 2, hw, hh)
        const hub: [number, number] = [hud.offsetLeft, hud.offsetTop]
        for (const m of MODES) {
          const card = el.querySelector<HTMLElement>(`.option--${m.corner}`)
          if (!card) continue
          const from: [number, number] = [card.offsetLeft + card.offsetWidth / 2, card.offsetTop + card.offsetHeight / 2]
          const [x2, y2] = edgeOf(from, hub, hubRect)
          wires.push({ corner: m.corner, hue: m.hue, x1: from[0], y1: from[1], x2, y2 })
        }
      }
      setLayout({ w, h, tear, wires })
    }

    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    // The hub's size depends on its type; re-measure once the display face is in.
    document.fonts?.ready.then(measure).catch(() => {})
    return () => ro.disconnect()
  }, [origin, host])

  if (!layout) return null
  const { w, h, tear, wires } = layout
  const tearPath = `M${tear.split(', ').join('L')}Z`

  return (
    <svg className="choose__backdrop" viewBox={`0 0 ${w} ${h}`} width={w} height={h} aria-hidden="true">
      {/* Past the rim it is a shade darker: the wall, seen from inside the hole. */}
      <path d={`M0 0H${w}V${h}H0Z ${tearPath}`} fill="rgba(0, 0, 0, 0.3)" fillRule="evenodd" />
      <path className="tear-glow" d={tearPath} fill="none" stroke="rgba(168, 85, 247, 0.07)" strokeWidth="14" />
      <path className="tear-rim" d={tearPath} fill="none" stroke="rgba(216, 180, 254, 0.28)" strokeWidth="1.2" />

      {wires.map((wr) => (
        <g key={wr.corner} className={`wire wire--${wr.corner}`} style={{ '--hue': wr.hue } as React.CSSProperties}>
          <line className="wire__glow" x1={wr.x1} y1={wr.y1} x2={wr.x2} y2={wr.y2} />
          <line className="wire__line" x1={wr.x1} y1={wr.y1} x2={wr.x2} y2={wr.y2} />
          <circle className="wire__node" cx={wr.x2} cy={wr.y2} r="3" />
        </g>
      ))}
    </svg>
  )
}
