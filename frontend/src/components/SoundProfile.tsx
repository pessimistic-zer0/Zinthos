import { profileOf } from '../lib/profile'

/**
 * What she heard: the query as the engine actually took it.
 *
 * Two readouts. The words, split the way `rules.parse` split them — the ones the rules
 * understood are solid, the ones they could not place are hollow, the same solid/hollow
 * split as ZIN and HOS on the landing. Whether hollow means "handed to the LLM" or simply
 * "dropped" depends on `source`, and the legend says which. Then the filters, one rail per
 * feature, lit over the stretch the filters left open.
 *
 * This is the thing the mode is FOR — searching by how music sounds — and until now the
 * console threw it away and showed only the tracks it produced.
 */

export interface Heard {
  matched?: string[]
  unmatched?: string[]
  source: string
  coverage?: number
  filters: unknown[]
}

export default function SoundProfile({ heard }: { heard: Heard }) {
  const { rails, genres } = profileOf(heard.filters)
  const llm = heard.source.includes('llm')
  const words = [
    ...(heard.matched ?? []).map((w) => ({ w, ok: true })),
    ...(heard.unmatched ?? []).map((w) => ({ w, ok: false })),
  ]
  if (words.length === 0 && rails.length === 0 && genres.length === 0) return null

  return (
    <div className="heard">
      {words.length > 0 && (
        <div className="heard__words">
          {words.map(({ w, ok }, i) => (
            <span
              key={`${w}-${i}`}
              className={`heard__word${ok ? '' : ' heard__word--hollow'}`}
              style={{ '--i': i } as React.CSSProperties}
            >
              {w}
            </span>
          ))}
          <span className="heard__legend">
            <i className="heard__key" /> rules
            <i className="heard__key heard__key--hollow" /> {llm ? 'the LLM' : 'not understood'}
            {typeof heard.coverage === 'number' && <b>{Math.round(heard.coverage * 100)}% parsed</b>}
          </span>
        </div>
      )}

      {(rails.length > 0 || genres.length > 0) && (
        <div className="heard__rails">
          {rails.map((r, i) => (
            <div
              key={r.column}
              className={`rail${r.empty ? ' rail--empty' : ''}`}
              style={
                {
                  '--lo': r.lo,
                  '--hi': r.hi,
                  '--i': i,
                } as React.CSSProperties
              }
              title={`${r.column} ${r.text}`}
            >
              <span className="rail__label">{r.label}</span>
              <span className="rail__track">
                <span className="rail__lit" />
                <span className="rail__end rail__end--lo">{r.ends[0]}</span>
                <span className="rail__end rail__end--hi">{r.ends[1]}</span>
              </span>
              <span className="rail__value">{r.empty ? 'none' : r.text}</span>
            </div>
          ))}
          {genres.length > 0 && (
            <div className="rail rail--genre">
              <span className="rail__label">Genre</span>
              <span className="heard__genres">
                {genres.map((g) => (
                  <span key={g}>{g}</span>
                ))}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
