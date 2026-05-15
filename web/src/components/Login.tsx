// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Lock, User, AlertCircle } from 'lucide-react'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { useTheme } from '../contexts/ThemeContext'
import { Button, Input, Label, Alert } from './ui'

interface LoginProps {
  onLogin: (username: string, password: string) => Promise<void>
  error?: string | null
}

export default function Login({ onLogin, error }: LoginProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const { isDark } = useTheme()

  const handleSubmit = async (e: React.FormEvent) => {
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
          {error && (
            <Alert variant="error" className="mb-6 flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0" />
              <p className="text-sm">{error}</p>
            </Alert>
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
              className="w-full shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-ring"
            >
              {isLoading ? 'Signing in...' : 'Sign in'}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
