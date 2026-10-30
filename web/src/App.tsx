// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useParams } from 'react-router'
import SetPassword from './components/SetPassword'
import SsoComplete from './components/SsoComplete'
import Login from './components/Login'
import ProjectSelection from './components/ProjectSelection'
import RequireAdmin from './components/RequireAdmin'
import { ReloadOnNewVersion } from './components/NewVersion'
import AppShell from './components/layout/AppShell'
import Overview from './pages/Overview'
import HostsPage from './pages/HostsPage'
import FailuresPage from './pages/FailuresPage'
import ProxiesPage from './pages/ProxiesPage'
import ConnectorsPage from './pages/ConnectorsPage'
import CredentialsPage from './pages/CredentialsPage'
import InspectorPage from './pages/InspectorPage'
import LocationsPage from './pages/LocationsPage'
import DevicesPage from './pages/DevicesPage'
import ConnectPage from './pages/ConnectPage'
import AccountSection from './pages/settings/AccountSection'
import AppearanceSection from './pages/settings/AppearanceSection'
import UsersSection from './pages/settings/UsersSection'
import BackupSection from './pages/settings/BackupSection'
import ProvidersSection from './pages/settings/ProvidersSection'
import SystemSection from './pages/settings/SystemSection'
import GeoSection from './pages/settings/GeoSection'
import WireGuardSection from './pages/settings/WireGuardSection'
import OpenVpnSection from './pages/settings/OpenVpnSection'
import SignInSection from './pages/settings/SignInSection'
import { ProjectProvider } from './contexts/ProjectContext'
import { TimeRangeProvider } from './contexts/TimeRangeContext'
import { AuthProvider, AuthContextValue } from './contexts/AuthContext'
import { ToastProvider } from './contexts/ToastContext'
import { NavigationGuardProvider } from './contexts/NavigationGuardContext'
import { useTheme } from './contexts/ThemeContext'
import { checkAuthStatus, login, logout, AuthStatus } from './api/client'

function App() {
  return (
    <BrowserRouter>
      <ReloadOnNewVersion />
      <Routes>
        {/* Unauthenticated route - accessible without login */}
        <Route path="/set-password/:token" element={<SetPassword />} />
        {/* Where a provider sends the browser back after sign-in */}
        <Route path="/sso/complete" element={<SsoComplete />} />
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

  return (
    <AuthProvider value={authContextValue}>
      <ProjectProvider>
        <TimeRangeProvider>
        <ToastProvider>
          <NavigationGuardProvider>
          <Routes>
            {/* Project selection page */}
            <Route path="/" element={<ProjectSelection />} />

            {/* Your own account: no project in scope */}
            <Route path="/account" element={<AppShell area="account" onLogout={handleLogout} />}>
              <Route index element={<AccountSection />} />
              <Route path="appearance" element={<AppearanceSection />} />
            </Route>

            {/* Administration: install-wide, every project */}
            <Route path="/admin" element={<RequireAdmin><AppShell area="admin" onLogout={handleLogout} /></RequireAdmin>}>
              <Route index element={<Navigate to="users" replace />} />
              <Route path="users" element={<UsersSection />} />
              <Route path="sign-in" element={<SignInSection />} />
              <Route path="backup" element={<BackupSection />} />
              <Route path="providers" element={<ProvidersSection />} />
              <Route path="geo" element={<GeoSection />} />
              <Route path="wireguard" element={<WireGuardSection />} />
              <Route path="openvpn" element={<OpenVpnSection />} />
              <Route path="system" element={<SystemSection />} />
            </Route>
            <Route path="/settings/*" element={<LegacySettingsRedirect />} />
            <Route path="/projects/:projectId/settings/*" element={<LegacySettingsRedirect />} />

            {/* Project-scoped pages */}
            <Route path="/projects/:projectId" element={<AppShell onLogout={handleLogout} />}>
              <Route index element={<Navigate to="overview" replace />} />
              <Route path="overview" element={<Overview />} />
              <Route path="hosts" element={<HostsPage />} />
              <Route path="failures" element={<FailuresPage />} />
              {/* Legacy routes from the previous navigation */}
              <Route path="dashboard" element={<Navigate to="../overview" replace />} />
              <Route path="metrics" element={<Navigate to="../overview" replace />} />
              <Route path="proxies" element={<ProxiesPage />} />
              <Route path="connectors" element={<ConnectorsPage />} />
              <Route path="credentials" element={<CredentialsPage />} />
              <Route path="connect" element={<ConnectPage />} />
              <Route path="mitm-inspector" element={<InspectorPage />} />
              <Route path="locations" element={<LocationsPage />} />
              <Route path="devices" element={<DevicesPage />} />
              <Route path="*" element={<Navigate to="../overview" replace />} />
            </Route>

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </NavigationGuardProvider>
        </ToastProvider>
        </TimeRangeProvider>
      </ProjectProvider>
    </AuthProvider>
  )
}

/** Settings links from before Account and Administration had their own addresses, inside a project or not. */
function LegacySettingsRedirect() {
  const page = useParams()['*'] ?? ''
  if (page === '' || page === 'account') return <Navigate to="/account" replace />
  if (page === 'appearance') return <Navigate to="/account/appearance" replace />
  return <Navigate to={`/admin/${page}`} replace />
}

export default App
