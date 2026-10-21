// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { AlertCircle } from 'lucide-react'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { useTheme } from '../contexts/ThemeContext'
import { Alert } from './ui'
import { exchangeSsoCode } from '../api/client'

/**
 * Where the browser lands after signing in with a provider. The URL carries
 * a one-time code, not the token: this page trades it for the token with
 * one API call and moves on. Reloading the page or opening the URL again
 * finds the code spent and shows the way back to the login page.
 */
export default function SsoComplete() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const { isDark } = useTheme()
  const [error, setError] = useState<string | null>(null)
  // React strict mode mounts twice in development; the code is single use.
  const started = useRef(false)

  useEffect(() => {
    if (started.current) return
    started.current = true
    const code = params.get('code')
    if (!code) {
      setError('This link is missing its sign-in code.')
      return
    }
    // The app shell mounts fresh at / and reads the stored token itself.
    exchangeSsoCode(code)
      .then(() => navigate('/', { replace: true }))
      .catch((err: any) => setError(err.message || 'The sign-in could not be completed.'))
  }, [params, navigate])

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg">
      <div className="max-w-md w-full">
        <div className="bg-surface rounded-lg shadow-lg p-8">
          <div className="text-center mb-6">
            <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-12 mx-auto" />
          </div>
          {error ? (
            <>
              <Alert variant="error" className="flex items-center gap-3">
                <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />
                <p className="text-sm">{error}</p>
              </Alert>
              <p className="text-sm text-fg-muted text-center mt-4">
                <Link to="/" replace className="text-primary hover:underline">Back to sign in</Link>
              </p>
            </>
          ) : (
            <p className="text-sm text-fg-muted text-center">Completing sign-in…</p>
          )}
        </div>
      </div>
    </div>
  )
}
