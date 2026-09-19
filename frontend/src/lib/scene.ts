/**
 * One shared flag for the landing's per-frame loops.
 *
 * When the about panel has scrolled up over the whole viewport the landing is still mounted
 * (it is the fixed backdrop the panel slides over), but nothing of it can be seen. Its
 * canvases keep their rAF loops alive so they resume instantly, and skip drawing while this
 * is set. Set from the scroll handler in `Landing`.
 */
export const scene = { covered: false }
