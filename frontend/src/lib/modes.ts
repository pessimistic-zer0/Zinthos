/**
 * The four doors on the other side of the warp.
 *
 * Each card is one real engine capability wearing a Raven persona: the art and the
 * voice are decoration, `kind` is the thing that decides which endpoint runs. Adding a
 * fifth mode means adding an entry here plus a branch in QueryModal.
 */

import { isDemo } from './demo'

export type ModeKind = 'vibe' | 'similar' | 'playlist' | 'library'

/**
 * What to call the thing being searched. The hosted demo runs the same engine over a slice
 * of the catalogue (see lib/demo.ts), so the "255M tracks" claim would be wrong there; the
 * exact figure is stated once by DemoNotice rather than repeated in every mode's copy.
 */
const CATALOG = isDemo ? 'A slice of the catalogue' : '255M tracks'

export interface Mode {
  id: ModeKind
  /** Corner it occupies in the matrix, matching the mockup's numbering. */
  corner: 'tl' | 'tr' | 'bl' | 'br'
  badge: string
  index: string
  name: string
  tagline: string
  /** Persona name, kept so the art still reads as a character rather than a stock photo. */
  persona: string
  /** What Raven says once the modal opens. */
  speech: string
  placeholder: string
  /** One line under the input explaining what the engine will actually do. */
  hint: string
  /** What this door can do, in plain words, shown in the console's persona column. */
  abilities: string[]
  card: string
  full: string
  /**
   * Where her eyes are in each piece of art, as an `object-position` pair.
   *
   * Every persona frame is square, and nothing that renders one is: the card's photo is
   * landscape (1 / --photo-ratio), and the console's is a tall column on a desktop and a
   * short banner on a phone. `object-fit: cover` therefore always crops, and its default
   * `50% 50%` crops around the middle of the FRAME — which in art where she sits high is
   * her chest. At the banner heights the stacked console uses, that removed her face from
   * the picture altogether.
   *
   * `object-position: X% Y%` aligns the Y% line of the image with the Y% line of the box,
   * so passing the eye line keeps it in view at every crop, and the shorter the box gets
   * the more exactly the eyes fill it. Measured off the art, not guessed — the two 03
   * values differ because its card and full crops are framed differently.
   */
  focus: { card: string; full: string }
  /** Accent hue (CSS hue angle) used for this card's glow and selection state. */
  hue: number
}

export const MODES: readonly Mode[] = [
  {
    id: 'vibe',
    corner: 'tl',
    badge: 'OPTION 01',
    index: '#01',
    name: 'Vibe Search',
    tagline: 'Describe it however it comes out',
    persona: 'Disbelief Raven',
    speech: '"You want it in words? Fine. Try me."',
    placeholder: 'rainy 3am drive, warm bass, nothing cheerful',
    hint: `Rules parse what they can; the LLM covers the rest. ${CATALOG}, ranked by feel.`,
    abilities: [
      'Describe the music in your own words.',
      'Moods, moments and feelings all work.',
      'Get songs that match the feeling, not the name.',
    ],
    card: '/assets/personas/card-01.jpg',
    full: '/assets/personas/full-01.jpg',
    focus: { card: '47% 27%', full: '47% 27%' },
    hue: 278,
  },
  {
    id: 'similar',
    corner: 'tr',
    badge: 'OPTION 02',
    index: '#02',
    name: 'Similar by Name',
    tagline: 'Name one track, get its neighbours',
    persona: 'Rebel Chic Raven',
    speech: '"Name it. I\'ll tell you what it runs with."',
    placeholder: 'Teardrop — Massive Attack',
    hint: 'Resolves the name to catalog copies first, then walks the learned embedding space.',
    abilities: [
      'Name one song you love.',
      'Get songs that sound like it.',
      'The closest matches come first.',
    ],
    card: '/assets/personas/card-02.jpg',
    full: '/assets/personas/full-02.jpg',
    focus: { card: '55% 52%', full: '55% 52%' },
    hue: 196,
  },
  {
    id: 'playlist',
    corner: 'bl',
    badge: 'OPTION 03',
    index: '#03',
    name: 'Playlist Builder',
    tagline: 'A sequence, not a pile',
    persona: 'Blushing Smirk Raven',
    speech: '"Give me the arc. I\'ll order the rest."',
    placeholder: 'slow start, builds to something euphoric, 30 tracks',
    hint: 'Same filters as vibe search, then sequenced so the energy curve holds.',
    abilities: [
      'Describe the mood and how long you want it.',
      'Get a playlist that flows from start to finish.',
      'A mix of songs you know and songs you don\'t.',
    ],
    card: '/assets/personas/card-03.jpg',
    full: '/assets/personas/full-03.jpg',
    focus: { card: '46% 36%', full: '46% 33%' },
    hue: 326,
  },
  {
    id: 'library',
    corner: 'br',
    badge: 'OPTION 04',
    index: '#04',
    name: 'Local Library',
    tagline: 'Start from what you already own',
    persona: 'Cozy Slouch Raven',
    speech: '"Show me your shelf. I\'ll find the gaps."',
    placeholder: 'Title — Artist   (one per line; add an ISRC at the end if you have one)',
    hint: 'Names are matched fuzzily against the catalogue; an ISRC on the line matches exactly. Seeds from the hits.',
    abilities: [
      'Paste in songs you already have.',
      'Add an ISRC per line for an exact match — optional.',
      'See what fits alongside them, and what is missing.',
    ],
    card: '/assets/personas/card-04.jpg',
    full: '/assets/personas/full-04.jpg',
    focus: { card: '43% 50%', full: '43% 50%' },
    hue: 30,
  },
]

export const modeById = (id: ModeKind): Mode =>
  MODES.find((m) => m.id === id) ?? (MODES[0] as Mode)
