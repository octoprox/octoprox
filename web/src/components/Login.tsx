// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Lock, User, AlertCircle } from 'lucide-react'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { useTheme } from '../contexts/ThemeContext'
import { Button, Input, Label, Alert } from './ui'
import { fetchSsoProviders, ssoStartUrl, PublicSsoProvider } from '../api/client'
import { ProviderMark } from './SignInProviderMark'

interface LoginProps {
  onLogin: (username: string, password: string) => Promise<void>
  error?: string | null
}

/** The reason a sign-in through a provider ended back here, if it did. Read once and cleared from the URL. */
function takeSsoError(): string | null {
  const url = new URL(window.location.href)
  const message = url.searchParams.get('sso_error')
  if (message === null) return null
  url.searchParams.delete('sso_error')
  window.history.replaceState(null, '', url.pathname + url.search + url.hash)
  return message
}

export default function Login({ onLogin, error }: LoginProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [ssoError, setSsoError] = useState<string | null>(null)
  const { isDark } = useTheme()
  const { data: providers = [] } = useQuery<PublicSsoProvider[]>({
    queryKey: ['sso-providers'], queryFn: fetchSsoProviders, staleTime: 60_000, retry: false,
  })

  useEffect(() => { setSsoError(takeSsoError()) }, [])
  const shownError = error ?? ssoError

  const handleSubmit = async (e: React.SubmitEvent) => {
    e.preventDefault()
    setIsLoading(true)
    try {
      await onLogin(username, password)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg relative">
      <div className="max-w-md w-full">
        <div className="bg-surface rounded-lg shadow-lg p-8">
          {/* Logo */}
          <div className="text-center mb-8">
            <div className="flex items-center justify-center mb-2">
              <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-12" />
            </div>
            <p className="text-fg-muted">Sign in to your account</p>
          </div>

          {/* Error message */}
          {shownError && (
            <Alert variant="error" className="mb-6 flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />
              <p className="text-sm">{shownError}</p>
            </Alert>
          )}

          {/* Sign in through a provider: a top-level navigation, the provider
              sends the browser back to /sso/complete with a one-time code. */}
          {providers.length > 0 && (
            <div className="mb-6">
              <div className="space-y-2">
                {providers.map((p) => (
                  <a
                    key={p.kind}
                    href={ssoStartUrl(p.kind)}
                    className="flex items-center justify-center gap-3 w-full h-10 rounded-lg border border-line-strong bg-surface text-sm font-medium text-fg hover:bg-surface-raised transition-colors"
                  >
                    <ProviderMark kind={p.kind} className="w-4 h-4" />
                    Continue with {p.label}
                  </a>
                ))}
              </div>
              <div className="flex items-center gap-3 mt-6 text-[11px] uppercase tracking-wider text-fg-subtle">
                <span className="flex-1 border-t border-line" />
                or sign in with a password
                <span className="flex-1 border-t border-line" />
              </div>
            </div>
          )}

          {/* Login form */}
          <form onSubmit={handleSubmit} className="space-y-6">
            <div>
              <Label htmlFor="username" className="mb-2">Username</Label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <User className="h-5 w-5 text-gray-400" />
                </div>
                <Input
                  id="username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="pl-10 focus:ring-2"
                  placeholder="Enter your username"
                  required
                  autoComplete="username"
                />
              </div>
            </div>

            <div>
              <Label htmlFor="password" className="mb-2">Password</Label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <Lock className="h-5 w-5 text-gray-400" />
                </div>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="pl-10 focus:ring-2"
                  placeholder="Enter your password"
                  required
                  autoComplete="current-password"
                />
              </div>
            </div>

            <Button
              type="submit"
              disabled={isLoading}
              className="w-full shadow-xs focus:outline-hidden focus:ring-2 focus:ring-offset-2 focus:ring-ring"
            >
              {isLoading ? 'Signing in...' : 'Sign in'}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
