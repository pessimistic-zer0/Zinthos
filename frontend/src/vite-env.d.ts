/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Point the client at a hosted engine instead of the dev proxy. */
  readonly VITE_ENGINE_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
