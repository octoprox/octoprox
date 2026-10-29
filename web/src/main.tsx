// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from './contexts/ThemeContext'
import App from './App'
import './index.css'

// After a release the server no longer has this build's lazily loaded files
// (the world map), so a page that needs one loads the new UI instead. Once:
// a file still missing just after that reload is not a release, and another
// reload would loop, so its error shows instead.
const PRELOAD_RELOAD_AT = 'octoprox_preload_reload_at'
window.addEventListener('vite:preloadError', (event) => {
  try {
    if (Date.now() - Number(sessionStorage.getItem(PRELOAD_RELOAD_AT)) < 10_000) return
    sessionStorage.setItem(PRELOAD_RELOAD_AT, String(Date.now()))
  } catch {
    return
  }
  event.preventDefault()
  window.location.reload()
})

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // No implicit polling: each query that needs live data sets its own interval.
      refetchInterval: false,
      staleTime: 1000,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
)

