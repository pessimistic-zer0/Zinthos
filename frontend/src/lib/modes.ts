/**
 * The four doors on the other side of the warp.
 *
 * Each card is one real engine capability wearing a Raven persona: the art and the
 * voice are decoration, `kind` is the thing that decides which endpoint runs. Adding a
 * fifth mode means adding an entry here plus a branch in QueryModal.
 */

export type ModeKind = 'vibe' | 'similar' | 'playlist' | 'library'

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
    hint: 'Rules parse what they can; the LLM covers the rest. 255M tracks, ranked by feel.',
    abilities: [
      'Describe the music in your own words.',
      'Moods, moments and feelings all work.',
      'Get songs that match the feeling, not the name.',
    ],
    card: '/assets/personas/card-01.jpg',
    full: '/assets/personas/full-01.jpg',
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
    placeholder: 'paste one "Title — Artist" per line',
    hint: 'Matches your files against the catalog by ISRC, then fuzzily, and seeds from the hits.',
    abilities: [
      'Paste in songs you already have.',
      'See what fits alongside them.',
      'Find what your collection is missing.',
    ],
    card: '/assets/personas/card-04.jpg',
    full: '/assets/personas/full-04.jpg',
    hue: 30,
  },
]

export const modeById = (id: ModeKind): Mode =>
  MODES.find((m) => m.id === id) ?? (MODES[0] as Mode)
