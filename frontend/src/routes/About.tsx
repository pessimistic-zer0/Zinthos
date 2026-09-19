import { useCallback, useEffect, useRef, useState } from 'react'
import ConstellationCanvas from '../components/ConstellationCanvas'
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
  {
    n: '04',
    hue: 30,
    title: 'Start from your shelf',
    body:
      "Paste in what you already own. Each line is matched to the catalogue by ISRC first, then by name, and the hits become the seed \u2014 so it recommends from your shelf, not a stranger's.",
  },
]

const TRACKS = 255_000_000

const TICKER = [
  '255,000,000 tracks',
  '348,000,000 artist links',
  '13 audio features',
  '10-number signatures',
  'under 100 ms',
  'search by feel',
]

/** The two halves of the comparison: what had to be handled, and what it ran on. */
const SCALE = [
  ['255,000,000', 'tracks'],
  ['348,000,000', 'artist links'],
  ['266 GB', 'of raw source data'],
]

const MACHINE = [
  ['15 GB', 'of memory'],
  ['6 GB', 'of video memory'],
  ['1', 'laptop'],
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
 * A figure inside running prose: the display serif, one size up from the sentence around it.
 * Bodoni is unreadable as an isolated numeral at this size, but a number in a sentence is
 * held up by the words on either side of it, so the hairlines cost nothing here.
 */
function N({ children }: { children: React.ReactNode }) {
  return <span className="fig">{children}</span>
}

/** 1.8s brief pause so people can read the hero text before continuing */
const PAUSE_DURATION = 200

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
  const big = useRef<HTMLParagraphElement>(null)

  const [isPaused, setIsPaused] = useState(false)
  const [hasPaused, setHasPaused] = useState(false)
  /** The sky fades in once the page has actually slid up over the landing, and back out
      when the rift is the whole view again. */
  const [skyOn, setSkyOn] = useState(false)
  const isPausedRef = useRef(false)
  const hasPausedRef = useRef(false)
  const pauseTimerRef = useRef<number | null>(null)
  const isTransitioningRef = useRef(false)
  const transitionFallbackTimer = useRef<number | null>(null)
  const touchStartY = useRef<number | null>(null)

  // Only while this panel exists may the document scroll; the warp and the matrix are
  // fixed screens and must not be scrolled out from under the rift.
  useEffect(() => {
    document.body.classList.add('is-scrollable')
    return () => {
      document.body.classList.remove('is-scrollable')
      window.scrollTo(0, 0)
    }
  }, [])

  const getHeroTop = useCallback(() => root.current?.offsetTop ?? window.innerHeight, [])

  const startTransition = useCallback((targetY: number) => {
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    isTransitioningRef.current = true
    window.scrollTo({ top: targetY, behavior: prefersReduced ? 'auto' : 'smooth' })
    if (transitionFallbackTimer.current !== null) {
      window.clearTimeout(transitionFallbackTimer.current)
    }
    transitionFallbackTimer.current = window.setTimeout(() => {
      isTransitioningRef.current = false
      transitionFallbackTimer.current = null
    }, 1200)
  }, [])

  const triggerPause = useCallback(() => {
    if (hasPausedRef.current || isPausedRef.current) return
    isPausedRef.current = true
    setIsPaused(true)

    if (pauseTimerRef.current !== null) {
      window.clearTimeout(pauseTimerRef.current)
    }
    pauseTimerRef.current = window.setTimeout(() => {
      isPausedRef.current = false
      hasPausedRef.current = true
      setIsPaused(false)
      setHasPaused(true)
      pauseTimerRef.current = null
    }, PAUSE_DURATION)
  }, [])

  const unpauseAndReset = useCallback(() => {
    if (pauseTimerRef.current !== null) {
      window.clearTimeout(pauseTimerRef.current)
      pauseTimerRef.current = null
    }
    if (transitionFallbackTimer.current !== null) {
      window.clearTimeout(transitionFallbackTimer.current)
      transitionFallbackTimer.current = null
    }
    isPausedRef.current = false
    hasPausedRef.current = false
    isTransitioningRef.current = false
    setIsPaused(false)
    setHasPaused(false)
  }, [])

  // If page is loaded or refreshed already past hero, mark as having paused
  useEffect(() => {
    const heroTop = getHeroTop()
    if (window.scrollY > heroTop + 50) {
      hasPausedRef.current = true
      setHasPaused(true)
    }
  }, [getHeroTop])

  // Wheel interception: pauses at hero covering the viewport
  useEffect(() => {
    const onWheel = (e: WheelEvent) => {
      const heroTop = getHeroTop()
      const scrollY = window.scrollY

      // Rearm if back at landing top
      if (scrollY <= 10 && e.deltaY < 0) {
        hasPausedRef.current = false
        setHasPaused(false)
      }

      // Scrolling DOWN
      if (e.deltaY > 0) {
        // While paused at hero, block scrolling down past hero
        if (isPausedRef.current) {
          e.preventDefault()
          window.scrollTo(0, heroTop)
          return
        }

        // If transitioning from landing down to hero, consume wheel ticks so smooth scroll completes
        if (isTransitioningRef.current && scrollY < heroTop - 8) {
          e.preventDefault()
          return
        }

        // User on landing scrolls down: glide to heroTop
        if (!hasPausedRef.current && scrollY < heroTop - 15) {
          e.preventDefault()
          startTransition(heroTop)
          return
        }

        // Arriving right at heroTop
        if (!hasPausedRef.current && Math.abs(scrollY - heroTop) <= 15) {
          e.preventDefault()
          window.scrollTo(0, heroTop)
          triggerPause()
          return
        }
      }

      // Scrolling UP
      if (e.deltaY < 0) {
        // While paused, allow user to return to landing
        if (isPausedRef.current) {
          unpauseAndReset()
          e.preventDefault()
          startTransition(0)
          return
        }

        // While transitioning up, consume wheel ticks so smooth scroll completes
        if (isTransitioningRef.current && scrollY > 10) {
          e.preventDefault()
          return
        }

        // If at hero and scrolling up, glide back to landing
        if (scrollY <= heroTop + 15 && scrollY >= heroTop - 15) {
          e.preventDefault()
          unpauseAndReset()
          startTransition(0)
          return
        }
      }
    }

    window.addEventListener('wheel', onWheel, { passive: false })
    return () => window.removeEventListener('wheel', onWheel)
  }, [getHeroTop, startTransition, triggerPause, unpauseAndReset])

  // Touch handlers for mobile
  useEffect(() => {
    const onTouchStart = (e: TouchEvent) => {
      const touch = e.touches[0]
      if (touch) touchStartY.current = touch.clientY
    }

    const onTouchMove = (e: TouchEvent) => {
      const touch = e.touches[0]
      if (touchStartY.current === null || !touch) return
      const currentY = touch.clientY
      const deltaY = touchStartY.current - currentY // > 0 means swiping up (scrolling down)
      const heroTop = getHeroTop()
      const scrollY = window.scrollY

      if (deltaY > 15) {
        if (isPausedRef.current) {
          e.preventDefault()
          window.scrollTo(0, heroTop)
          return
        }
        if (isTransitioningRef.current && scrollY < heroTop - 8) {
          e.preventDefault()
          return
        }
        if (!hasPausedRef.current && scrollY < heroTop - 15) {
          e.preventDefault()
          startTransition(heroTop)
          return
        }
      } else if (deltaY < -15) {
        if (isPausedRef.current) {
          unpauseAndReset()
          e.preventDefault()
          startTransition(0)
          return
        }
        if (scrollY <= heroTop + 15 && scrollY >= heroTop - 15) {
          e.preventDefault()
          unpauseAndReset()
          startTransition(0)
          return
        }
      }
    }

    const onTouchEnd = () => {
      touchStartY.current = null
    }

    window.addEventListener('touchstart', onTouchStart, { passive: true })
    window.addEventListener('touchmove', onTouchMove, { passive: false })
    window.addEventListener('touchend', onTouchEnd, { passive: true })
    return () => {
      window.removeEventListener('touchstart', onTouchStart)
      window.removeEventListener('touchmove', onTouchMove)
      window.removeEventListener('touchend', onTouchEnd)
    }
  }, [getHeroTop, startTransition, unpauseAndReset])

  // Keyboard handlers (ArrowDown, PageDown, Space)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const heroTop = getHeroTop()
      const scrollY = window.scrollY

      if (e.key === 'ArrowDown' || e.key === 'PageDown' || e.key === ' ') {
        if (isPausedRef.current) {
          e.preventDefault()
          return
        }
        if (!hasPausedRef.current && scrollY < heroTop - 15) {
          e.preventDefault()
          startTransition(heroTop)
          return
        }
      }

      if (e.key === 'ArrowUp' || e.key === 'PageUp') {
        if (isPausedRef.current) {
          unpauseAndReset()
          e.preventDefault()
          startTransition(0)
          return
        }
        if (scrollY <= heroTop + 15 && scrollY >= heroTop - 15) {
          e.preventDefault()
          unpauseAndReset()
          startTransition(0)
          return
        }
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [getHeroTop, startTransition, unpauseAndReset])

  // Scroll listener: clamps during pause, triggers on arrival from smooth scroll or button
  useEffect(() => {
    const onScroll = () => {
      const heroTop = getHeroTop()
      const scrollY = window.scrollY

      // At top of landing
      if (scrollY <= 10) {
        if (hasPausedRef.current) {
          hasPausedRef.current = false
          setHasPaused(false)
        }
        isTransitioningRef.current = false
      }

      // If at or reaching heroTop
      if (Math.abs(scrollY - heroTop) <= 6) {
        isTransitioningRef.current = false
        if (transitionFallbackTimer.current !== null) {
          window.clearTimeout(transitionFallbackTimer.current)
          transitionFallbackTimer.current = null
        }
        if (!hasPausedRef.current && !isPausedRef.current) {
          triggerPause()
        }
      }

      // If paused, clamp scroll position to heroTop
      if (isPausedRef.current && scrollY > heroTop) {
        window.scrollTo(0, heroTop)
      }

      // The stars belong to this page; they only cost frames once part of it is showing.
      setSkyOn((on) => {
        const next = scrollY > window.innerHeight * 0.25
        return next === on ? on : next
      })
    }

    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [getHeroTop, triggerPause])

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (pauseTimerRef.current !== null) {
        window.clearTimeout(pauseTimerRef.current)
      }
      if (transitionFallbackTimer.current !== null) {
        window.clearTimeout(transitionFallbackTimer.current)
      }
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
      const groups = Array.from(el.querySelectorAll<HTMLSpanElement>('.big__group'))
      // Padded to nine digits so the stack keeps its width for the whole run: an odometer,
      // not three numbers that grow a digit at a time and shove the paragraph sideways.
      const paint = (v: number) => {
        const d = String(v).padStart(9, '0')
        groups.forEach((g, i) => {
          g.textContent = d.slice(i * 3, i * 3 + 3)
        })
      }
      const t0 = performance.now()
      const DUR = 1600
      const tick = (now: number) => {
        const t = Math.min(1, (now - t0) / DUR)
        const k = 1 - Math.pow(2, -10 * t) // ease-out, fast start, long settle
        paint(Math.round(TRACKS * k))
        if (t < 1) raf = requestAnimationFrame(tick)
        else paint(TRACKS)
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

      {/* The same sky as the landing and the matrix, seen from below: this page slides
          over her, but it never leaves her world. */}
      <div className={`about__stars${skyOn ? ' is-visible' : ''}`} aria-hidden="true">
        <ConstellationCanvas intensity={0.55} alwaysOn />
        <span className="grid-veil" />
      </div>

      <div className="about__col">
        {/* 00 — the claim */}
        <header className="opening">
          <p className="chapter reveal">
            <span className="chapter__n">00</span>
            <span className="chapter__name">What this is</span>
            <span className={`chapter__note${isPaused ? ' is-paused' : ''}`}>
              {isPaused && <span className="chapter__note-dot" />}
              {isPaused ? 'Reading' : 'Scroll'}
            </span>
          </p>
          <h2 className="opening__title reveal" id="about-title">
            <Lines lines={['Music,', 'before', 'the name.']} hollowFrom={1} />
          </h2>
          <div className="opening__row reveal">
            <div className="opening__indicator" aria-hidden="true">
              {isPaused ? (
                <span className="hud-pill opening__badge">
                  <span className="pulse-dot" /> Brief pause · Read text
                </span>
              ) : hasPaused ? (
                <span className="hud-pill opening__badge is-ready">
                  <span>Scroll to continue</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </span>
              ) : null}
            </div>
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
        {/* 01 — four ways in */}
        <section className="ways" aria-labelledby="ways-title">
          <p className="chapter reveal">
            <span className="chapter__n">01</span>
            <span className="chapter__name" id="ways-title">Four ways in</span>
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
          <div className="numbers__body">
            <p className="big reveal" ref={big}>
              {/* Three groups, one per line, behind the sentence. Spoken as one number just
                  below, so a screen reader is not read "255" and two zeroes as three
                  separate figures. */}
              {/* Stack and unit share a row so they top-align to each other, while the row
                  as a whole stays centred on the paragraph. */}
              <span className="big__figure" aria-hidden="true">
                <span className="big__stack">
                  <span className="big__group">255</span>
                  <span className="big__group">000</span>
                  <span className="big__group">000</span>
                </span>
                {/* The unit, set down the side of the figure one letter at a time. */}
                <span className="big__word">
                  {['t', 'r', 'a', 'c', 'k', 's'].map((c, i) => (
                    <span className="big__letter" key={`${c}${i}`}>
                      {c}
                    </span>
                  ))}
                </span>
              </span>
              <span className="sr-only">{TRACKS.toLocaleString('en-US')} tracks in the catalogue</span>
            </p>
            <p className="figures reveal">
              You type a line. Rules parse what they can and a language model writes the rest,
              and what comes back are filters over the <N>13</N> audio features every track
              carries — tempo, energy, valence and ten more. Those are squeezed to a{' '}
              <N>10</N>-number signature, so <em>near</em> means near in sound rather than near
              in spelling. A FAISS index walks all <N>254.8 million</N> of them, returns its
              closest hundred in under <N>100 ms</N>, and holds <N>94.3%</N> of the true top
              ten. A re-ranker orders what survives against <N>348 million</N> artist links and
              the genre a track carries, where it has one: <N>125 million</N> arrived labelled,
              a classifier earned <N>49 million</N> more, and <N>81 million</N> are left blank
              rather than guessed.
            </p>
          </div>
        </section>

        {/* 03 — the size of the problem against the size of the machine */}
        <section className="how" aria-labelledby="how-title">
          {/* Her silhouette in the page's violet, behind the comparison: she came through with
              you on the matrix screen, and she never really left. Cut off at the section's
              bottom edge so she does not run down into the footer. */}
          <div className="about__raven" aria-hidden="true" />
          <p className="chapter reveal">
            <span className="chapter__n chapter__n--chipped">03</span>
            <span className="chapter__name" id="how-title">What it had to fit in</span>
            <span className="chapter__note">The whole project, in one comparison</span>
          </p>
          <div className="gap reveal">
            <div className="gap__side">
              <h3 className="gap__head">The catalogue</h3>
              <dl className="gap__rows">
                {SCALE.map(([v, label]) => (
                  <div className="gap__row" key={label}>
                    <dd className="gap__v">{v}</dd>
                    <dt className="gap__l">{label}</dt>
                  </div>
                ))}
              </dl>
            </div>
            <div className="gap__side gap__side--machine">
              <h3 className="gap__head">The machine</h3>
              <dl className="gap__rows">
                {MACHINE.map(([v, label]) => (
                  <div className="gap__row" key={label}>
                    <dd className="gap__v">{v}</dd>
                    <dt className="gap__l">{label}</dt>
                  </div>
                ))}
              </dl>
            </div>
          </div>
          <p className="gap__coda reveal">
            Every choice here is the same choice, made over and over: give up something you can
            measure to stay inside that second column. Hard compression was tried, and the search
            found one track in three. Smaller rows were tried, and the first load took two and a
            half hours instead of four minutes. What shipped is what fit.
          </p>
        </section>

        {/* End */}
        <footer className="end">
          <div className="end__row reveal">
            <p className="end__line">She is still guarding it.</p>
            <span className="about__sig">Zinthos Archive // 255M indexed</span>
            <button type="button" className="ghost-btn end__back" onClick={backToTop}>
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
