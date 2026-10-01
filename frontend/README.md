# Zinthos Frontend

The portal: a landing screen you jump *through* into a four-way mode selector, each mode
wired to a real Engine endpoint.

## Run

```sh
npm install
npm run dev        # http://127.0.0.1:5173
```

The dev server proxies `/api/*` to the Engine at `http://127.0.0.1:3000`
(`backend/engine/config.py` defaults). Start the Engine separately:

```sh
python -m backend.engine.main      # or however you normally run `sonic serve`
```

Point at a different Engine with `SONIC_ENGINE=http://host:port npm run dev`, or, for a
production build, `VITE_ENGINE_BASE=https://…` at build time.

The landing screen works with the Engine down — the track-count pill falls back to the
catalog figure, and a query then reports `engine unreachable` in the console panel.

Other scripts: `npm run build`, `npm run preview`, `npm run typecheck`.

## Shape

```
index.html
src/
  App.tsx                 phase machine: landing → warp → choose, plus #/choose and #/drift/{id}
  routes/Landing.tsx      the portal; clicking Raven opens the rift she is guarding
  routes/Choose.tsx       the 4-corner decision matrix
  components/
    ChooseBackdrop        the options page's tear (the rift polygon at screen size, rim lit,
                          wall darker beyond it) and the matrix wires from each card to
                          the hub; hover/selection light a wire via :has() in choose.css
    ConstellationCanvas   drifting starfield that links up around the pointer; on the
                          landing (intensity 1) and the options page (1.5: the collage swallows faint stars)
    RiftCanvas            the crack at rest behind Raven
    RiftReveal            the crack opening — clip polygon + edge light, one clock for both
    QueryModal            per-mode query console + results; 80vw x 80vh, swoops in from the
                          card that was clicked (Choose passes the card's centre as --from-*)
                          persona column lists the mode's `abilities` from lib/modes.ts;
                          results are a stack of views (search → neighbours → …) with a trail
    TrackRow              one result: cover, preview, neighbours, a link out to the full track
    SoundProfile          "what she heard": parsed words solid/hollow, filters as lit rails
    RiftSpectrum          the live spectrum drawn as the crack — opens with the music
    NowPlaying            the pill that keeps the queue once the console has closed
  routes/Drift.tsx        Drift, the listening mode — any track becomes an endless radio
  lib/
    api.ts                typed Engine client (shapes mirror engine/hydrate.py)
    player.ts             two decks, a crossfader, one analyser, and the queue
    drift/                Drift's engine: radio (the controller), field (the paint),
                          painter (WebGL2), source (a cover as 112×112 sorted homes)
    profile.ts            filter triples → one [lo, hi] interval per feature
    modes.ts              the 4 modes — art, voice, and which endpoint each one runs
    useRavenEvasion.ts    Raven drifts away from the pointer
    rift.ts               the crack's geometry, opening curve and clip string
    cursor.ts  warp.ts    shared pointer state; rift timing read back from CSS
  styles/                 global tokens + one sheet per screen
raven_images/             the Raven art, full resolution — the source of truth
public/assets/            what ships: hero, the 4 personas, the collage background
tools/build-collage.sh    regenerates public/assets/collage-bg.jpg from raven_images/
mockups/                  the original Stitch exports, kept for reference
```

## The art

Every image is served from disk out of `public/assets/` — nothing loads off a CDN at
runtime. The originals live in `raven_images/` and each one maps to exactly one slot:

| Source | Ships as | Used by |
| --- | --- | --- |
| `raven main page.png` | `assets/raven-hero.png` | the landing portal |
| `vibe search.jpg` | `assets/personas/{card,full}-01.jpg` | mode 01 |
| `similar by name.jpg` | `assets/personas/{card,full}-02.jpg` | mode 02 |
| `playlist builder.jpg` | `assets/personas/{card,full}-03.jpg` | mode 03 |
| `local library.jpg` | `assets/personas/{card,full}-04.jpg` | mode 04 |
| the remaining 7 | `assets/collage-bg.jpg` | the choose screen backdrop |

`card-*` is a 640px square and `full-*` keeps the native frame; both are cropped by
`object-fit: cover` at render, so the square source is just headroom. The grid's own
crop is `1 / var(--photo-ratio)` (landscape, 0.82), not square — see below.

The hero keeps its alpha (PNG) and its framing: the crack is sized and placed from the
rendered box (`riftFromLayout` in `lib/rift.ts` — centred, 1.2× her height, capped to the stage), so it survives
a resolution change only as long as the subject still fills the canvas edge to edge. It
does — check with `magick <file> -alpha extract -threshold 1% -format '%@' info:` before
swapping her out.

The collage is pre-dimmed to ~0.17 mean brightness because `.choose__ambient` adds its own
gradient on top; `tools/build-collage.sh` bakes that in. It is a JPEG, not a PNG — no alpha
is needed and PNG cost 467kB for the same fidelity JPEG gives in 189kB.

## The rift

Raven is guarding a crack in the dimension wall. It sits behind her on the landing — a
dark tear a fifth taller than she is, a sliver of black darker than the wall with a
hairline of light along its edge, leaning so it shows past her silhouette. The light is on
the rim; the hole is the other side. It is calm, not dead: hairline fractures spread off
it, one dim pulse travels its edge, a tendril snaps off now and then, and motes fall in and
go dark as they cross the edge. Click her and it takes her: the wall gives, she recoils,
then she is pulled in, and the tear grows until it has swallowed the screen. The matrix
is what was on the other side.

Both screens live in one document so nothing unloads mid-animation. The mechanics:

- `lib/rift.ts` is the one geometry. `riftPolygon(g, k, reach)` gives the crack's outline at
  opening `k` (0 = closed seam, `K_MAX` = past every corner) from a seeded vertex list, so
  the idle crack and the opening tear are the same shape. `riftFromLayout` places it from the
  stage and Raven's art.
- `RiftCanvas` draws it at rest, inside the landing behind her (bleed, fractures, seam,
- `components/ShieldFrame.tsx` + `lib/shield.ts` — the shield she holds over the frame:
  a seeded rounded pane with fifteen bites and nicks out of its rim (one corner taken
  off), raw white edges inside each bite, forking fractures from the bites and from the
  intact rim — a few of them long stress cracks — worn stretches where the rim has gone
  dark, and shards floating outside. What says *barrier* rather than *window*: a hexagonal
  energy lattice under the glass, hidden in the middle and lit toward the rim and around
  the three hardest hits (an SVG luminance mask), ripple rings at those hits, and threads
  of energy from the orbs in her hands to the rim — drawn on `RiftCanvas` because she
  moves (`ORB`, `ANCHORS`).
- The wordmark is set letter by letter (`.wordmark__letter` in `landing.css`): N and H lean
  into the rift with a static skew, and Z, N, O, S carry chips cut with `clip-path` polygons
  in percentages of the letter box, so the damage scales with the type. ZIN is solid,
  HOS is hollow (`-webkit-text-stroke`) — the far half is already half gone. Each letter's
  colour is a `--ink` variable, which `RiftCanvas` reads to colour the fragments that
  drift from every chip (`data-chip`, a point in the letter box) to the nearest point on
  the seam. On the jump, `launch` writes each letter's vector to the seam into `--to-x/y`
  and `letter-pulled` drags them in, N and H first.
- `routes/About.tsx` + `styles/about.css` — the page under the landing: what Zinthos is and
  how it is built. A spec sheet: numbered chapter rules over hairlines, a three-line
  Bodoni claim (line two hollow, like HOS on the shield) whose lines rise out of clipped
  boxes, a ticker band of the numbers (one transform, running only while on screen), three
  doors with oversized hollow numerals in their option hues, the track count counting up
  once on first sight, the pipeline as a rail that draws in (`scaleX`) with nodes dropping
  on, and the full name ZINTHOS hollow across the foot with the T solid and falling into
  its gap. Normal flow, `margin-top: 100vh`, z-index above the fixed landing, so scrolling
  slides it up over her. `overflow: clip` (not hidden) crops the foot without making the
  section a scroll container. Mounted only in the landing phase; while mounted it sets
  `body.is-scrollable` (body is `overflow: hidden` otherwise, the warp and matrix are
  fixed screens). Sections fade up once via one IntersectionObserver. When the panel
  covers the whole viewport the landing gets `is-covered` (visibility hidden) and both
  canvases skip drawing via the shared `lib/scene.ts` flag.
- The missing T is her. ZIN and HOS spell the name without its T, and her pose is one.
  `.raven__t` is a hollow T inside the `.raven` wrapper, a fifth taller than she is (from
  `--raven-h`, set by a ResizeObserver on the hero), crossbar on her arms, leaning 12° so
  its stem runs with the crack. It brightens on hover and leaves with her on the jump. Resized via ResizeObserver;
  geometry re-rolls identically because the seed is fixed.
  one pulse, rare tendrils, motes — in that order), and reports the measured geometry back so the
  launch opens exactly that crack.
- `RiftReveal` owns the clock during the jump. Each frame it writes the polygon to
  `--rift-clip` on `<html>`, which `.choose.is-warp-arrival` uses as its `clip-path`, and
  paints what the clip cannot: the flash as the wall gives, the lit edges, the branches dying,
  and debris streaking *inward* — the cue that says sucked rather than revealed. One loop for
  clip and light is deliberate: started a frame apart they drift, and at peak the edge moves
  thousands of px/s.
- Raven's recoil-then-pull, the sigil going with her, and the landing being dragged toward
  the crack are CSS keyframes in `landing.css`, timed against `CRACK_UNTIL` in `rift.ts`
  (the crack snaps open for the first 28%, then the pull ramps in cubically).

The matrix settles from a slight overscale (1.12 → 1) as it is revealed; its cards sit
paused on frame 0 until the hold ends, then are thrown at the camera (`card-drop`, 55ms
apart).

`prefers-reduced-motion: reduce` skips the rift entirely and goes straight to `#/choose`.

### Performance rules

The whole UI runs on one principle: **paint-time effects are cached, compositor effects are
not.** `text-shadow`, `box-shadow` and gradients are rasterised once into tiles. `filter:`
and `backdrop-filter:` are re-applied by the GPU on every frame the compositor draws — and
with two canvases and a floating Raven, it draws every frame. So:

- No `filter: blur()` / `drop-shadow()` on anything that is on screen at rest. Glows are
  radial gradients; the wordmark halo is a `text-shadow` on a pseudo-element under the
  gradient glyphs.
- Transitions animate `transform` and `opacity` only. The old warp animated a 36px blur on
  the full page while scaling it to 90x, and the matrix ran a second 30px blur under it.
- No `backdrop-filter`, even for glass. Raven's shield (`ShieldFrame.tsx`) is a static
  inline SVG: four fills for the pane (sheen, tiled noise grain, a glint band, violet edge
  haze) and strokes for the rim. Painted once. Frosted blur would re-run over the whole
  viewport every frame.
- No canvas `shadowBlur`. The constellation draws pre-rendered glow sprites with
  `drawImage`; the rift fakes glow with wide low-alpha strokes under a thin bright one.
- No `will-change` at rest; it is set on the landing only while it recedes.

### Small screens

Three breakpoints, and each one exists because something specific broke at it.

- **1100px** — the four corners collide with the centre copy, so the matrix becomes a
  scrollable stack. Between 641px and 1100px the cards pair up two abreast.
- **900px** — the query console's two panes stack. Source order puts the persona first,
  which stacked is ~520px of photo, speech and bullets *above* the only input on the
  screen. `display: contents` on `.console__persona` dissolves its box so its parts become
  items of the console in their own right and can be ordered independently: photo and name
  stay on top, the query and its results come next, the speech and the ability list keep
  their place below.
- **640px** — the console goes full-bleed and its close button goes `position: fixed`;
  Raven gives up width so `ZIN` and `HOS` have somewhere to sit; the demo notice docks to
  the bottom edge as a bar and the footers reserve room for it (`body:has(.demo-notice)`).

Two traps worth knowing, both of which cost real debugging here:

- **A grid with a definite height shrinks its `auto` rows rather than overflowing.** It
  sizes them between min-content and max-content and, when the total does not fit, takes
  the difference out of the rows. A stacked console is `position: fixed` and therefore has
  a definite height, so the results row collapsed to 428px while holding 3104px of rows and
  the list spilled over everything below it. Either use a column flex container (the
  console) or pin the rows with `grid-auto-rows: max-content` (the matrix).
- **`will-change: transform` establishes a containing block for `position: fixed`
  descendants**, with or without a transform set — the same as a transform itself does.
  The console's close button was pinned to the console and scrolled off the top until the
  hint was cleared at that breakpoint.

`dvh`, not `vh`, wherever the mobile address bar matters: `vh` is measured as though the bar
were already hidden, so `.about`'s `margin-top: 100vh` left a dead strip under the landing.
`dvh` agrees with `window.innerHeight`, which is what `Landing.tsx` compares `scrollY`
against to decide the landing is covered.

Touch is a pointer that cannot hover, so `lib/cursor.ts` ignores anything that is not a
mouse or a pen. A touch fires `pointermove` too, and `pointerleave` does not fire for a
finger that simply lifted — so Raven would flinch away from the last tap and stay there for
the rest of the session. Target sizes key off `@media (hover: none)` rather than a width: a
phone in landscape is wider than some laptops, and a touchscreen laptop is neither.

## The four modes

| Card | Mode | Endpoint |
| --- | --- | --- |
| 01 | Vibe Search | `GET /search` |
| 02 | Similar by Name | `GET /search/by-name` → pick a copy → `GET /search/similar/{id}` |
| 03 | Playlist Builder | `POST /playlist` |
| 04 | Local Library | `POST /library/scan` |

Modes are data (`src/lib/modes.ts`); a fifth means an entry there plus a branch in
`QueryModal`.

## The console, listening

The engine has always returned more than the console drew. Every record carries
`cover_art_url` and `preview_url` (Spotify's 30-second clips), a search carries what its parser
made of the words (`matched`, `unmatched`, `coverage`, `source`) and the predicates it ran
(`filters`), and a library scan carries a genre and decade `breakdown`. The console used to
render titles and nothing else. Now:

- **Every row plays.** `lib/player.ts` owns one `<audio>` routed through a Web Audio analyser,
  and a queue: click a row and the list plays on from there, skipping tracks the catalogue has
  no preview for. "Play the sequence" in mode 03 is the playlist as ordered. Closing the
  console does not stop it — `NowPlaying` carries it on the matrix — but leaving through the
  rift does.
- **The analyser works only because of one header.** p.scdn.co answers with
  `Access-Control-Allow-Origin: *`, and `crossOrigin = 'anonymous'` is set on the element
  before its first `src`. Without both, a MediaElementSource outputs silence rather than leak
  cross-origin samples: the music would play and every spectrum would stay flat.
- **The rift listens.** `RiftSpectrum` is the spectrum drawn as the crack: a hairline seam at
  silence, lips parting band by band (log-spaced, low on the left, pinned shut at both ends)
  as the music plays. It crosses her photo in the console and sits in the pill. On the
  matrix, the tear's rim breathes with the low end — `TearPulse`, a second copy of the rim on
  its own layer whose opacity alone changes, so the compositor fades a raster it already has.
- **What she heard.** Above the results, the query as the engine took it: words the rules
  placed are solid, words they could not are hollow (the ZIN/HOS split) — handed to the LLM
  or dropped, as `source` says. Each feature's filters collapse to the interval they leave
  open and are drawn as a lit stretch of a rail: `valence < 350` is "Mood: dark ← .35".
- **Wander.** Any result opens its own neighbourhood (`/search/similar/{id}`), pushed on top of
  the current view; the trail above the list walks back. Mode 02's "pick a copy" is the same
  move, replacing its picker rather than leaving it in the trail. Vibe search also pages:
  "Dig deeper" asks for the next 30 at the next offset.
- **Thumbnails at 2 kB.** Spotify serves every cover at 64, 300 and 640 px under one path
  segment; `coverAt()` swaps it, so a list of 30 costs ~60 kB of art rather than ~3 MB.

Per-frame values — the spectrum, the progress hairline, the rim's opacity — are read from
`player` inside each canvas's or element's own rAF and written straight to the DOM. Only the
coarse state (which track, playing or not) goes through React, via `useSyncExternalStore`.

## Drift: the listening mode

Every door ends in a track, and **pressing play is Drift**: a row, its cover, or the results
bar's Play opens it, the song's paint pouring out of the row it was pressed in. From then on
the radio never stops — each song is followed by one of its own neighbours in the embedding,
four offered beside the painting. It is `#/drift/{track_id}`; a pasted link opens straight
into it (the seed comes from `GET /track/{id}`).

**Back to the list folds Drift into the rift** (so do Esc and the back button). The radio
keeps playing and choosing; only the screen goes — pulled into the listening seam on
Raven's photo in the console, or the pill's seam when the console is closed.
`lib/drift/fold.ts` runs it on one clock that writes Drift's transform and draws the light on
a canvas above it: the screen **recoils**, is **crushed** to a band whose edges light as rift
rims while the room darkens and debris streaks at the crack, holds as a **trembling line**,
and **snaps** in — flash, twin shockwave rings, the console jolts, the seam tears wide and
bounces shut. The music goes under a lowpass as it is pulled in (over a rising whoosh) and
opens back up on the sub thud of the landing (`fx` in `lib/player.ts`; the filter sits after
the analyser, so the spectra keep showing the music). **Back to Drift** in the results bar,
the pill's *drift*, or play on the song on air tears it back out the same way in reverse.
While folded, *next* is Drift's next song and *stop* ends it. The matrix only goes dark once
Drift reports it fully covers the screen — mid-fold, the console must still be there around
the band. `window.__foldSlow = 10` (dev builds only) plays the fold at a tenth of the speed.

The same thing exists standalone, on paper, in `../frontend-drift/`, and that README
explains the mechanics: the sorted-luminance morph, the three forces, paint that dries,
and why the auto-drift leans "nearby" and avoids repeating an artist. What changed on the
way in:

- **One audio graph.** Drift does not bring its own decks. `lib/player.ts` became two decks
  behind the same API (`play`, `toggle`, `spectrum`, `level`, `progress`), plus `mix(track,
  fade)` for Drift and an `onNearEnd` hook that fires 3.2 s before a clip ends. The console,
  the rift spectrum, the tear pulse and the pill kept working unchanged, and they now get
  real crossfades. Only the live deck's events count. The outgoing deck pauses after its
  fade and eventually ends, and either event, taken at face value, would report the new
  song as paused or push the queue on. While Drift is steering, the queue does not advance
  on its own: the end of a song is Drift's to handle.
- **Night, not paper.** The painter takes its ground as an option; here it is `--bg`, and
  the wash dries paint back to it. The paper version's accent (tuned for ink, lightness
  36–50%) is lifted to 72% lightness so it reads on dark.
- **The repo's performance rules.** The title swap that blurs in the paper version fades and
  rises here: transform and opacity only. While Drift covers the matrix, the matrix is
  `visibility: hidden` (after Drift's 0.35 s fade) and its starfield skips drawing through the
  shared `scene.covered` flag, so nothing composites under an opaque screen.
- **It pours from where you chose it.** The first picture pours out of the console row's
  cover (or the pill's disc); every switch after that pours out of the chosen way's disc.
- **It lives in the matrix, not in App.** `Choose` holds the Drift state, so the console
  underneath keeps its search, its trail and its scroll. Folded, Drift stays mounted — it is
  still the radio — and its painting stops drawing until it is torn back out.
