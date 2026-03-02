// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Lock, AlertCircle, CheckCircle, Moon, Sun } from 'lucide-react'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { useTheme } from '../contexts/ThemeContext'
import { Button, Input, Label, Alert } from './ui'
import { setPasswordWithToken } from '../api/client'

export default function SetPassword() {
  const { token } = useParams<{ token: string }>()
  const navigate = useNavigate()
  const { theme, toggleTheme } = useTheme()
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    if (password !== confirmPassword) {
      setError('Passwords do not match')
      return
    }

    if (password.length < 6) {
      setError('Password must be at least 6 characters')
      return
    }

    if (!token) {
      setError('Invalid invite link')
      return
    }

    setIsLoading(true)
    try {
      await setPasswordWithToken(token, password)
      setSuccess(true)
      // Brief delay to show success message, then redirect
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent('auth:login'))
        navigate('/')
      }, 1500)
    } catch (err: any) {
      setError(err.message || 'Failed to set password. The invite link may be invalid or expired.')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900 relative">
      {/* Theme toggle */}
      <button
        onClick={toggleTheme}
        className="absolute top-4 right-4 p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-white/60 dark:hover:bg-gray-800/60 rounded-lg transition-colors"
        title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      >
        {theme === 'dark' ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
      </button>
      <div className="max-w-md w-full">
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
          {/* Logo */}
          <div className="text-center mb-8">
            <div className="flex items-center justify-center mb-2">
              <img src={theme === 'dark' ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-12" />
            </div>
            <p className="text-gray-500 dark:text-gray-400">Set your password to get started</p>
          </div>

          {/* Success message */}
          {success && (
            <Alert variant="success" className="mb-6 flex items-center gap-3">
              <CheckCircle className="w-5 h-5 text-green-500 flex-shrink-0" />
              <p className="text-sm">Password set successfully! Redirecting...</p>
            </Alert>
          )}

          {/* Error message */}
          {error && (
            <Alert variant="error" className="mb-6 flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0" />
              <p className="text-sm">{error}</p>
            </Alert>
          )}

          {/* Set password form */}
          {!success && (
            <form onSubmit={handleSubmit} className="space-y-6">
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
                    placeholder="Choose a password"
                    required
                    autoComplete="new-password"
                  />
                </div>
              </div>

              <div>
                <Label htmlFor="confirm-password" className="mb-2">Confirm Password</Label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                    <Lock className="h-5 w-5 text-gray-400" />
                  </div>
                  <Input
                    id="confirm-password"
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    className="pl-10 focus:ring-2"
                    placeholder="Confirm your password"
                    required
                    autoComplete="new-password"
                  />
                </div>
              </div>

              <Button
                type="submit"
                disabled={isLoading}
                className="w-full shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 dark:focus:ring-violet-500"
              >
                {isLoading ? 'Setting password...' : 'Set Password'}
              </Button>
            </form>
          )}
        </div>
      </div>
    </div>
  )
}
