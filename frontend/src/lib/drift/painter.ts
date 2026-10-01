/**
 * Paint on a ground, in raw WebGL2. (Copied from frontend-drift/, where the ground is paper; here it is night.)
 *
 * Three passes a frame, into one accumulation buffer that is never cleared:
 *
 *   1. wash     a full-screen quad of paper colour at low alpha — the paint drying. The
 *               lower the alpha, the longer a stroke stays wet on the page.
 *   2. paint    every particle as a soft round dab at its current position. A still
 *               particle re-dabs the same spot and converges on its colour; a moving one
 *               leaves a trail that the wash slowly takes back.
 *   3. present  the buffer to the screen, with paper grain added in the shader.
 *
 * The buffer is half-float where the GPU can render to it. In 8-bit, a wash of alpha 0.05
 * cannot finish its job — once a pixel is within ~10 levels of the paper, 5% of the gap
 * rounds to nothing — and every trail leaves a permanent grey ghost. That is also, at a
 * lower level, what real paper does, so the 8-bit fallback is kept rather than refused.
 */
import { N } from './source'
import type { Field } from './field'

export type RGB = [number, number, number]

export interface PainterOptions {
  /** The ground the paint sits on, and the colour the wash dries it back to. */
  ground: RGB
  /** Strength of the tooth added in the present pass. Paper wants more than night does. */
  grain: number
}

const FULLSCREEN_VS = /* glsl */ `#version 300 es
out vec2 vUv;
void main() {
  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
  vUv = p;
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`

const WASH_FS = /* glsl */ `#version 300 es
precision mediump float;
uniform vec3 uPaper;
uniform float uAlpha;
out vec4 o;
void main() { o = vec4(uPaper, uAlpha); }`

const DAB_VS = /* glsl */ `#version 300 es
layout(location = 0) in vec2 aPos;
layout(location = 1) in vec4 aCol;
uniform vec2 uRes;
uniform vec2 uCentre;
uniform float uHalf;
uniform float uSize;
out vec4 vCol;
void main() {
  vec2 px = uCentre + aPos * uHalf;
  gl_Position = vec4(px.x / uRes.x * 2.0 - 1.0, 1.0 - px.y / uRes.y * 2.0, 0.0, 1.0);
  gl_PointSize = uSize;
  vCol = aCol;
}`

const DAB_FS = /* glsl */ `#version 300 es
precision mediump float;
in vec4 vCol;
out vec4 o;
void main() {
  float d = length(gl_PointCoord - 0.5);
  float a = smoothstep(0.5, 0.28, d) * vCol.a * 0.92;
  if (a < 0.01) discard;
  o = vec4(vCol.rgb, a);
}`

const PRESENT_FS = /* glsl */ `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uTex;
uniform float uSeed;
uniform float uGrain;
out vec4 o;
float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}
void main() {
  vec3 c = texture(uTex, vUv).rgb;
  // Paper: a fine static tooth, and a coarser, fainter mottling.
  float g = hash(floor(gl_FragCoord.xy)) - 0.5;
  float m = hash(floor(gl_FragCoord.xy / 7.0) + uSeed) - 0.5;
  c += (g * 0.028 + m * 0.012) * uGrain;
  o = vec4(c, 1.0);
}`

function compile(gl: WebGL2RenderingContext, vs: string, fs: string): WebGLProgram {
  const p = gl.createProgram()!
  for (const [type, src] of [
    [gl.VERTEX_SHADER, vs],
    [gl.FRAGMENT_SHADER, fs],
  ] as const) {
    const s = gl.createShader(type)!
    gl.shaderSource(s, src)
    gl.compileShader(s)
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? 'shader')
    gl.attachShader(p, s)
  }
  gl.linkProgram(p)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? 'link')
  return p
}

export interface Layout {
  /** Painting centre and half-size, CSS px. */
  cx: number
  cy: number
  half: number
}

export class Painter {
  private gl: WebGL2RenderingContext
  private wash: WebGLProgram
  private dab: WebGLProgram
  private present: WebGLProgram
  private vao: WebGLVertexArrayObject
  private posBuf: WebGLBuffer
  private colBuf: WebGLBuffer
  private fbo: WebGLFramebuffer | null = null
  private tex: WebGLTexture | null = null
  private half16: boolean
  private dpr = 1
  private w = 1
  private h = 1
  layout: Layout = { cx: 0, cy: 0, half: 100 }

  constructor(
    readonly canvas: HTMLCanvasElement,
    private readonly opts: PainterOptions,
  ) {
    const gl = canvas.getContext('webgl2', { antialias: false, alpha: false, preserveDrawingBuffer: false })
    if (!gl) throw new Error('WebGL2 is not available')
    this.gl = gl
    this.half16 = !!gl.getExtension('EXT_color_buffer_float') || !!gl.getExtension('EXT_color_buffer_half_float')
    this.wash = compile(gl, FULLSCREEN_VS, WASH_FS)
    this.dab = compile(gl, DAB_VS, DAB_FS)
    this.present = compile(gl, FULLSCREEN_VS, PRESENT_FS)

    this.vao = gl.createVertexArray()!
    gl.bindVertexArray(this.vao)
    this.posBuf = gl.createBuffer()!
    gl.bindBuffer(gl.ARRAY_BUFFER, this.posBuf)
    gl.bufferData(gl.ARRAY_BUFFER, N * 2 * 4, gl.DYNAMIC_DRAW)
    gl.enableVertexAttribArray(0)
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0)
    this.colBuf = gl.createBuffer()!
    gl.bindBuffer(gl.ARRAY_BUFFER, this.colBuf)
    gl.bufferData(gl.ARRAY_BUFFER, N * 4 * 4, gl.DYNAMIC_DRAW)
    gl.enableVertexAttribArray(1)
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 0, 0)
    gl.bindVertexArray(null)

    this.resize()
  }

  resize(): void {
    const gl = this.gl
    this.dpr = Math.min(window.devicePixelRatio || 1, 1.5)
    this.w = Math.max(1, Math.round(window.innerWidth * this.dpr))
    this.h = Math.max(1, Math.round(window.innerHeight * this.dpr))
    this.canvas.width = this.w
    this.canvas.height = this.h

    if (this.tex) gl.deleteTexture(this.tex)
    if (this.fbo) gl.deleteFramebuffer(this.fbo)
    this.tex = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    const [ifmt, type] = this.half16 ? [gl.RGBA16F, gl.HALF_FLOAT] : [gl.RGBA8, gl.UNSIGNED_BYTE]
    gl.texImage2D(gl.TEXTURE_2D, 0, ifmt, this.w, this.h, 0, gl.RGBA, type, null)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    this.fbo = gl.createFramebuffer()
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo)
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex, 0)
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE && this.half16) {
      // Advertised but not actually renderable on this driver: fall back and try again.
      this.half16 = false
      this.resize()
      return
    }
    gl.viewport(0, 0, this.w, this.h)
    const [r, g, b] = this.opts.ground
    gl.clearColor(r, g, b, 1)
    gl.clear(gl.COLOR_BUFFER_BIT)
    gl.bindFramebuffer(gl.FRAMEBUFFER, null)
  }

  /** `dry` 0-1: how much of the last frame's wet paint the wash takes back. */
  draw(field: Field, dry: number): void {
    const gl = this.gl
    const L = this.layout
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo)
    gl.viewport(0, 0, this.w, this.h)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)

    gl.useProgram(this.wash)
    gl.uniform3fv(gl.getUniformLocation(this.wash, 'uPaper'), this.opts.ground)
    gl.uniform1f(gl.getUniformLocation(this.wash, 'uAlpha'), dry)
    gl.drawArrays(gl.TRIANGLES, 0, 3)

    gl.bindVertexArray(this.vao)
    gl.bindBuffer(gl.ARRAY_BUFFER, this.posBuf)
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, field.pos)
    if (field.colourDirty) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.colBuf)
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, field.col)
      field.colourDirty = false
    }
    gl.useProgram(this.dab)
    gl.uniform2f(gl.getUniformLocation(this.dab, 'uRes'), this.w, this.h)
    gl.uniform2f(gl.getUniformLocation(this.dab, 'uCentre'), L.cx * this.dpr, L.cy * this.dpr)
    gl.uniform1f(gl.getUniformLocation(this.dab, 'uHalf'), L.half * this.dpr)
    // One dab a little wider than the grid pitch, so a still picture has no gaps.
    gl.uniform1f(gl.getUniformLocation(this.dab, 'uSize'), Math.max(2, ((L.half * 2) / 112) * this.dpr * 1.45))
    gl.drawArrays(gl.POINTS, 0, N)
    gl.bindVertexArray(null)

    gl.bindFramebuffer(gl.FRAMEBUFFER, null)
    gl.disable(gl.BLEND)
    gl.viewport(0, 0, this.w, this.h)
    gl.useProgram(this.present)
    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.uniform1i(gl.getUniformLocation(this.present, 'uTex'), 0)
    gl.uniform1f(gl.getUniformLocation(this.present, 'uSeed'), 3.7)
    gl.uniform1f(gl.getUniformLocation(this.present, 'uGrain'), this.opts.grain)
    gl.drawArrays(gl.TRIANGLES, 0, 3)
  }

  /** Free the GPU objects; the canvas itself belongs to the caller. */
  dispose(): void {
    const gl = this.gl
    gl.deleteTexture(this.tex)
    gl.deleteFramebuffer(this.fbo)
    gl.deleteBuffer(this.posBuf)
    gl.deleteBuffer(this.colBuf)
    gl.deleteVertexArray(this.vao)
    for (const p of [this.wash, this.dab, this.present]) gl.deleteProgram(p)
  }
}
