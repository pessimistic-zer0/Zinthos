/**
 * What the paint is trying to become: a picture, reduced to G×G pixels and sorted by
 * brightness.
 *
 * The sort is the whole trick of the morph. Particle j always takes the j-th darkest pixel
 * of whatever is showing, so when the picture changes, every particle keeps roughly its
 * shade and only has to travel to where the new picture needs that shade. The old cover's
 * shadows run to the new cover's shadows, its highlights to the new highlights: the image
 * rearranges itself instead of cross-dissolving.
 */

export const G = 112
export const N = G * G

export interface Source {
  /** Home positions in painting space, -1..1, y down. Sorted by luminance, darkest first. */
  x: Float32Array
  y: Float32Array
  /** Colour per home, 0-1 sRGB; alpha 0 means "paper here, stay invisible". */
  r: Float32Array
  g: Float32Array
  b: Float32Array
  a: Float32Array
  /** Mean colour, lifted, for the UI accents. */
  accent: string
}

function hsl(r: number, g: number, b: number): [number, number, number] {
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  if (max === min) return [230, 0, l]
  const d = max - min
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4
  return [h * 60, s, l]
}

function grid(): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const c = document.createElement('canvas')
  c.width = c.height = G
  const g = c.getContext('2d', { willReadFrequently: true })
  if (!g) throw new Error('2d canvas unavailable')
  return [c, g]
}

function build(data: Uint8ClampedArray, paperIsEmpty: boolean): Source {
  const lum = new Float32Array(N)
  const order = new Uint32Array(N)
  for (let i = 0; i < N; i++) {
    const r = data[i * 4]!
    const g = data[i * 4 + 1]!
    const b = data[i * 4 + 2]!
    // A hair of positional jitter breaks ties, so a flat colour field does not travel as
    // one rigid sheet in scan order.
    lum[i] = 0.2126 * r + 0.7152 * g + 0.0722 * b + Math.random() * 1.5
    order[i] = i
  }
  order.sort((p, q) => lum[p]! - lum[q]!)

  const s: Source = {
    x: new Float32Array(N),
    y: new Float32Array(N),
    r: new Float32Array(N),
    g: new Float32Array(N),
    b: new Float32Array(N),
    a: new Float32Array(N),
    accent: '#2a3cff',
  }
  let sr = 0
  let sg = 0
  let sb = 0
  let sw = 0
  for (let j = 0; j < N; j++) {
    const i = order[j]!
    const px = i % G
    const py = (i / G) | 0
    s.x[j] = ((px + 0.5) / G) * 2 - 1
    s.y[j] = ((py + 0.5) / G) * 2 - 1
    const r = data[i * 4]! / 255
    const g = data[i * 4 + 1]! / 255
    const b = data[i * 4 + 2]! / 255
    s.r[j] = r
    s.g[j] = g
    s.b[j] = b
    s.a[j] = paperIsEmpty ? (lum[i]! > 200 ? 0 : 1) : 1
    const sat = Math.max(r, g, b) - Math.min(r, g, b)
    sr += r * sat
    sg += g * sat
    sb += b * sat
    sw += sat
  }
  if (sw > 1) {
    // The cover's most saturated average, then forced into a band where it reads as a
    // colour against paper and ink: a near-black cover would otherwise hand the UI an
    // accent indistinguishable from the text.
    const [h, sat, l] = hsl(sr / sw, sg / sw, sb / sw)
    s.accent = `hsl(${Math.round(h)} ${Math.round(Math.max(0.55, sat) * 100)}% ${Math.round(Math.min(0.5, Math.max(0.36, l)) * 100)}%)`
  }
  return s
}

/** A cover, cropped square. Resolves null if it will not load (dead link, no CORS). */
export function fromImage(url: string): Promise<Source | null> {
  return new Promise((resolve) => {
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.decoding = 'async'
    img.onload = () => {
      const [, g] = grid()
      const s = Math.min(img.naturalWidth, img.naturalHeight)
      g.drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, G, G)
      try {
        resolve(build(g.getImageData(0, 0, G, G).data, false))
      } catch {
        resolve(null) // tainted: the host sent no CORS header
      }
    }
    img.onerror = () => resolve(null)
    img.src = url
  })
}

/**
 * A word set in ink on paper, for the painting to be before there is any music.
 * `style` is the shorthand's leading part and `family` its tail; the size is fitted here.
 * (Passing a whole shorthand and prefixing a size makes an invalid font string, which
 * canvas silently replaces with 10px sans-serif.)
 */
export function fromWord(word: string, family: string, style = 'italic 400'): Source {
  const [, g] = grid()
  g.fillStyle = '#fff'
  g.fillRect(0, 0, G, G)
  g.fillStyle = '#111'
  g.textAlign = 'center'
  g.textBaseline = 'middle'
  let size = 90
  g.font = `${style} ${size}px ${family}`
  while (g.measureText(word).width > G * 0.94 && size > 12) {
    size -= 2
    g.font = `${style} ${size}px ${family}`
  }
  g.fillText(word, G / 2, G / 2 + size * 0.06)
  const s = build(g.getImageData(0, 0, G, G).data, true)
  s.accent = '#2a3cff'
  return s
}

/** For a track with no usable cover: a field of one colour from its id, still paintable. */
export function fromSeed(id: number): Source {
  const [, g] = grid()
  const h = (id * 137.508) % 360
  const grad = g.createLinearGradient(0, 0, G, G)
  grad.addColorStop(0, `hsl(${h} 60% 38%)`)
  grad.addColorStop(1, `hsl(${(h + 60) % 360} 70% 62%)`)
  g.fillStyle = grad
  g.fillRect(0, 0, G, G)
  g.fillStyle = 'rgba(0,0,0,0.85)'
  g.beginPath()
  g.arc(G / 2, G / 2, G * 0.12, 0, Math.PI * 2)
  g.fill()
  return build(g.getImageData(0, 0, G, G).data, false)
}
