// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/// <reference types="vite/client" />

/** The release this UI was built from (vite.config.ts). */
declare const __OCTOPROX_VERSION__: string

declare module '*.svg' {
  const content: string
  export default content
}

declare module '*.png' {
  const content: string
  export default content
}

declare module '*.jpg' {
  const content: string
  export default content
}

