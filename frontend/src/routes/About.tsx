import { useEffect, useRef } from 'react'
import '../styles/about.css'

const WAYS = [
  {
    n: '01',
    hue: 278,
    title: 'Search by feeling',
    body:
      'Say it the way you would say it: slow, sad, late-night piano. The words become filters over the audio itself. When the words get strange, a language model writes the filters instead.',
  },
  {
    n: '02',
    hue: 196,
    title: 'Follow the sound',
    body:
      'Start from one track you love. Its sound is boiled down to a ten-number signature. The nearest hundred come back in under a tenth of a second, and a re-ranker hands you twenty.',
  },
  {
    n: '03',
    hue: 326,
    title: 'Build the journey',
    body:
      'Give it a mood and it builds the set: seven parts familiar, three parts deep cuts, ordered so tempo, energy and key move without a jolt.',
  },
]

const TRACKS = 255_000_000

const STATS = [
  ['400M', 'artist links'],
  ['13', 'audio features per track'],
  ['10', 'numbers per signature'],
  ['<100 ms', 'to the nearest neighbours'],
]

const TICKER = [
  '255,000,000 tracks',
  '400,000,000 artist links',
  '13 audio features',
  '10-number signatures',
  'under 100 ms',
  'search by feel',
]

const STEPS = [
  {
    title: 'Ingest',
    body: 'C++ parsers stream 266 GB of source data into one SQLite database under a 16 GB memory ceiling.',
  },
  {
    title: 'Label',
    body: 'A gradient-boosted model names a genre for the 113 million tracks that arrived without one, from audio alone.',
  },
  {
    title: 'Embed',
    body: 'A supervised autoencoder squeezes the features to ten numbers, arranged by how music sounds rather than what it is called.',
  },
  {
    title: 'Index',
    body: 'A FAISS index over every signature, so the nearest neighbours are a lookup, not a scan of 255 million rows.',
  },
  {
    title: 'Serve',
    body: 'A FastAPI backend answers in under two seconds. This page is its front door.',
  },
]

/** A headline split into lines that rise out of a clipped box, one after the other. */
function Lines({ lines, hollowFrom = 99 }: { lines: string[]; hollowFrom?: number }) {
  return (
    <>
      {lines.map((l, i) => (
        <span className="line" key={l} style={{ '--i': i } as React.CSSProperties}>
          <span className={`line__in${i >= hollowFrom ? ' is-hollow' : ''}`}>{l}</span>
        </span>
      ))}
    </>
  )
}

/**
 * The page under the landing: what Zinthos is and how it is built.
 *
 * A normal-flow section one viewport down, above the fixed landing in z-order, so scrolling
 * slides it up over her. Big type, hairlines and air; nothing paints per frame except the
 * ticker (one transform) and a one-off count-up on the big number. Sections reveal on first
 * sight through one IntersectionObserver that toggles a class.
 */
export default function About() {
  const root = useRef<HTMLElement>(null)
  const big = useRef<HTMLSpanElement>(null)

  // Only while this panel exists may the document scroll; the warp and the matrix are
  // fixed screens that must not be scrolled out from under the rift.
  useEffect(() => {
    document.body.classList.add('is-scrollable')
    return () => {
      document.body.classList.remove('is-scrollable')
      window.scrollTo(0, 0)
    }
  }, [])

  useEffect(() => {
    const el = root.current
    if (!el) return
    const items = el.querySelectorAll<HTMLElement>('.reveal')
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add('is-in')
            io.unobserve(e.target)
          }
        }
      },
      { rootMargin: '0px 0px -10% 0px', threshold: 0.12 },
    )
    items.forEach((i) => io.observe(i))

    // The ticker only moves while it is on screen.
    const ticker = el.querySelector<HTMLElement>('.ticker')
    const live = ticker
      ? new IntersectionObserver(([e]) => ticker.classList.toggle('is-live', !!e?.isIntersecting))
      : null
    if (ticker && live) live.observe(ticker)

    return () => {
      io.disconnect()
      live?.disconnect()
    }
  }, [])

  // The big number counts up once, the first time it is seen. It renders complete before
  // that so nothing ever shows a zero for longer than the animation itself.
  useEffect(() => {
    const el = big.current
    if (!el) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    let raf = 0
    const io = new IntersectionObserver(([e]) => {
      if (!e?.isIntersecting) return
      io.disconnect()
      const t0 = performance.now()
      const DUR = 1600
      const tick = (now: number) => {
        const t = Math.min(1, (now - t0) / DUR)
        const k = 1 - Math.pow(2, -10 * t) // ease-out, fast start, long settle
        el.textContent = Math.round(TRACKS * k).toLocaleString('en-US')
        if (t < 1) raf = requestAnimationFrame(tick)
        else el.textContent = TRACKS.toLocaleString('en-US')
      }
      raf = requestAnimationFrame(tick)
    }, { threshold: 0.4 })
    io.observe(el)
    return () => {
      io.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [])

  const backToTop = () => window.scrollTo({ top: 0, behavior: 'smooth' })

  return (
    <section className="about" id="about" ref={root} aria-labelledby="about-title">
      <div className="about__edge" aria-hidden="true" />
      <div className="about__glow" aria-hidden="true" />

      <div className="about__col">
        {/* 00 — the claim */}
        <header className="opening">
          <p className="chapter reveal">
            <span className="chapter__n">00</span>
            <span className="chapter__name">What this is</span>
            <span className="chapter__note">Scroll</span>
          </p>
          <h2 className="opening__title reveal" id="about-title">
            <Lines lines={['Music,', 'without', 'the box.']} hollowFrom={1} />
          </h2>
          <div className="opening__row reveal">
            <p className="opening__lead">
              Zinthos is a search engine over 255 million tracks. You do not need a name or a
              tag. Describe a sound or a mood and it finds the music that matches, then keeps
              going from there.
            </p>
          </div>
        </header>
      </div>

      {/* The band: the numbers, drifting. One transform on one element. */}
      <div className="ticker" aria-hidden="true">
        <div className="ticker__track">
          {[0, 1].map((copy) => (
            <span className="ticker__set" key={copy}>
              {TICKER.map((t) => (
                <span className="ticker__item" key={t}>
                  {t}
                  <i />
                </span>
              ))}
            </span>
          ))}
        </div>
      </div>

      <div className="about__col">
        {/* 01 — three ways in */}
        <section className="ways" aria-labelledby="ways-title">
          <p className="chapter reveal">
            <span className="chapter__n">01</span>
            <span className="chapter__name" id="ways-title">Three ways in</span>
            <span className="chapter__note">Pick the one that matches what you already know</span>
          </p>
          <div className="ways__grid">
            {WAYS.map((w, i) => (
              <article
                className="way reveal"
                key={w.n}
                style={{ '--i': i, '--hue': w.hue } as React.CSSProperties}
              >
                <span className="way__n" aria-hidden="true">{w.n}</span>
                <h3 className="way__title">{w.title}</h3>
                <p className="way__body">{w.body}</p>
              </article>
            ))}
          </div>
        </section>

        {/* 02 — by the numbers */}
        <section className="numbers" aria-labelledby="numbers-title">
          <p className="chapter reveal">
            <span className="chapter__n">02</span>
            <span className="chapter__name" id="numbers-title">By the numbers</span>
          </p>
          <p className="big reveal">
            <span className="big__value" ref={big}>{TRACKS.toLocaleString('en-US')}</span>
            <span className="big__label">tracks in the catalogue</span>
          </p>
          <dl className="stats reveal">
            {STATS.map(([v, label], i) => (
              <div className="stat" key={label} style={{ '--i': i } as React.CSSProperties}>
                <dd className="stat__value">{v}</dd>
                <dt className="stat__label">{label}</dt>
              </div>
            ))}
          </dl>
        </section>

        {/* 03 — how it is built */}
        <section className="how" aria-labelledby="how-title">
          <p className="chapter reveal">
            <span className="chapter__n">03</span>
            <span className="chapter__name" id="how-title">How it is built</span>
            <span className="chapter__note">Five stages, in order</span>
          </p>
          <ol className="rail reveal">
            <span className="rail__line" aria-hidden="true" />
            {STEPS.map((s, i) => (
              <li className="node" key={s.title} style={{ '--i': i } as React.CSSProperties}>
                <span className="node__dot" aria-hidden="true" />
                <span className="node__n">{String(i + 1).padStart(2, '0')}</span>
                <h3 className="node__title">{s.title}</h3>
                <p className="node__body">{s.body}</p>
              </li>
            ))}
          </ol>
        </section>

        {/* End */}
        <footer className="end">
          <div className="end__row reveal">
            <p className="end__line">She is still guarding it.</p>
            <button type="button" className="end__back" onClick={backToTop}>
              <span>Back to the rift</span>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="6 15 12 9 18 15" />
              </svg>
            </button>
          </div>
        </footer>
      </div>

      {/* The name, whole for once: on the landing she stands in for the T. Down here the
          letter itself drops into the gap. Cut off by the page edge on purpose. */}
      <p className="mark reveal" aria-hidden="true">
        <span className="mark__l">Z</span>
        <span className="mark__l">I</span>
        <span className="mark__l">N</span>
        <span className="mark__l mark__l--t">T</span>
        <span className="mark__l">H</span>
        <span className="mark__l">O</span>
        <span className="mark__l">S</span>
      </p>
    </section>
  )
}
