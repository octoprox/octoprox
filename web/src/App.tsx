// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import SetPassword from './components/SetPassword'
import Login from './components/Login'
import ProjectSelection from './components/ProjectSelection'
import RequireAdmin from './components/RequireAdmin'
import AppShell from './components/layout/AppShell'
import Overview from './pages/Overview'
import ProxiesPage from './pages/ProxiesPage'
import ConnectorsPage from './pages/ConnectorsPage'
import CredentialsPage from './pages/CredentialsPage'
import InspectorPage from './pages/InspectorPage'
import AccountSection from './pages/settings/AccountSection'
import AppearanceSection from './pages/settings/AppearanceSection'
import UsersSection from './pages/settings/UsersSection'
import BackupSection from './pages/settings/BackupSection'
import { ProjectProvider } from './contexts/ProjectContext'
import { AuthProvider, AuthContextValue } from './contexts/AuthContext'
import { ToastProvider } from './contexts/ToastContext'
import { useTheme } from './contexts/ThemeContext'
import { checkAuthStatus, login, logout, AuthStatus } from './api/client'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Unauthenticated route — accessible without login */}
        <Route path="/set-password/:token" element={<SetPassword />} />
        {/* All other routes require authentication */}
        <Route path="/*" element={<AuthenticatedApp />} />
      </Routes>
    </BrowserRouter>
  )
}

function AuthenticatedApp() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loginError, setLoginError] = useState<string | null>(null)
  const { applyServerTheme } = useTheme()

  const refreshAuth = async () => {
    try {
      const status = await checkAuthStatus()
      setAuthStatus(status)
      if (status.authenticated) {
        applyServerTheme(status.theme_preference as any)
      }
    } catch {
      setAuthStatus({ authenticated: false, username: null, role: null, user_id: null, theme_preference: null })
    }
  }

  useEffect(() => {
    const checkAuth = async () => {
      await refreshAuth()
      setIsLoading(false)
    }
    checkAuth()

    const handleLogout = () => {
      setAuthStatus((prev) => prev ? { ...prev, authenticated: false, username: null, role: null, user_id: null } : null)
    }
    const handleLogin = () => { refreshAuth() }

    window.addEventListener('auth:logout', handleLogout)
    window.addEventListener('auth:login', handleLogin)
    return () => {
      window.removeEventListener('auth:logout', handleLogout)
      window.removeEventListener('auth:login', handleLogin)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleLogin = async (username: string, password: string) => {
    setLoginError(null)
    try {
      await login(username, password)
      const status = await checkAuthStatus()
      setAuthStatus(status)
      if (status.authenticated) {
        applyServerTheme(status.theme_preference as any)
      }
    } catch (error: any) {
      setLoginError(error.response?.data?.detail || 'Login failed. Please try again.')
    }
  }

  const handleLogout = () => {
    logout()
    setAuthStatus((prev) => prev ? { ...prev, authenticated: false, username: null, role: null, user_id: null } : null)
  }

  const authContextValue = useMemo<AuthContextValue>(() => ({
    authStatus,
    isAdmin: authStatus?.role === 'admin',
    canMutate: authStatus?.role === 'admin' || authStatus?.role === 'editor',
    isViewer: authStatus?.role === 'viewer',
  }), [authStatus])

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-fg-muted text-sm">Loading…</div>
      </div>
    )
  }

  if (!authStatus?.authenticated) {
    return <Login onLogin={handleLogin} error={loginError} />
  }

  const settingsRoutes = (
    <>
      <Route index element={<Navigate to="account" replace />} />
      <Route path="account" element={<AccountSection />} />
      <Route path="appearance" element={<AppearanceSection />} />
      <Route path="users" element={<RequireAdmin><UsersSection /></RequireAdmin>} />
      <Route path="backup" element={<RequireAdmin><BackupSection /></RequireAdmin>} />
    </>
  )

  return (
    <AuthProvider value={authContextValue}>
      <ProjectProvider>
        <ToastProvider>
          <Routes>
            {/* Project selection page */}
            <Route path="/" element={<ProjectSelection />} />

            {/* Global settings (no project in scope) */}
            <Route path="/settings" element={<AppShell onLogout={handleLogout} />}>
              {settingsRoutes}
            </Route>

            {/* Project-scoped pages */}
            <Route path="/projects/:projectId" element={<AppShell onLogout={handleLogout} />}>
              <Route index element={<Navigate to="overview" replace />} />
              <Route path="overview" element={<Overview />} />
              {/* Legacy routes from the previous navigation */}
              <Route path="dashboard" element={<Navigate to="../overview" replace />} />
              <Route path="metrics" element={<Navigate to="../overview" replace />} />
              <Route path="proxies" element={<ProxiesPage />} />
              <Route path="connectors" element={<ConnectorsPage />} />
              <Route path="credentials" element={<CredentialsPage />} />
              <Route path="mitm-inspector" element={<InspectorPage />} />
              <Route path="settings">{settingsRoutes}</Route>
              <Route path="*" element={<Navigate to="overview" replace />} />
            </Route>

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </ToastProvider>
      </ProjectProvider>
    </AuthProvider>
  )
}

export default App
