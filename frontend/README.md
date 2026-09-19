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
  App.tsx                 phase machine: landing → warp → choose, plus #/choose hash routing
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
                          persona column lists the mode's `abilities` from lib/modes.ts
  lib/
    api.ts                typed Engine client (shapes mirror engine/hydrate.py)
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

## The four modes

| Card | Mode | Endpoint |
| --- | --- | --- |
| 01 | Vibe Search | `GET /search` |
| 02 | Similar by Name | `GET /search/by-name` → pick a copy → `GET /search/similar/{id}` |
| 03 | Playlist Builder | `POST /playlist` |
| 04 | Local Library | `POST /library/scan` |

Modes are data (`src/lib/modes.ts`); a fifth means an entry there plus a branch in
`QueryModal`.
