// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'
import { BrowserRouter, Routes, Route, Link, Navigate, useParams, useNavigate, useLocation } from 'react-router-dom'
import { Server, BarChart3, LineChart, LogOut, FolderOpen, ChevronDown, ChevronLeft, Key, Link2, Search, Settings as SettingsIcon, ArrowLeft, User as UserIcon, Palette, Users as UsersIcon } from 'lucide-react'
import SetPassword from './components/SetPassword'
import Dashboard from './components/Dashboard'
import octoproxLogo from './assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from './assets/logos/octoprox_horizontal_dark.svg'
import octoproxLogoOnly from './assets/logos/octoprox_logo_only.svg'
import octoproxLogoOnlyDark from './assets/logos/octoprox_logo_only_dark.svg'
import ProxyList from './components/ProxyList'
import CredentialsConfig from './components/CredentialsConfig'
import ConnectorConfig from './components/ConnectorConfig'
import MitmInspector from './components/MitmInspector'
import Metrics from './components/Metrics'
import Login from './components/Login'
import ProjectSelection from './components/ProjectSelection'
import RequireAdmin from './components/RequireAdmin'
import SettingsLayout from './pages/settings/SettingsLayout'
import AccountSection from './pages/settings/AccountSection'
import AppearanceSection from './pages/settings/AppearanceSection'
import UsersSection from './pages/settings/UsersSection'
import { ProjectProvider, useProject } from './contexts/ProjectContext'
import { AuthProvider, AuthContextValue } from './contexts/AuthContext'
import { useTheme } from './contexts/ThemeContext'
import { checkAuthStatus, login, logout, AuthStatus } from './api/client'
import { popSettingsOrigin, setSettingsOrigin } from './utils/settingsOrigin'

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

  // Check auth status on mount
  useEffect(() => {
    const checkAuth = async () => {
      await refreshAuth()
      setIsLoading(false)
    }
    checkAuth()

    // Listen for logout events
    const handleLogout = () => {
      setAuthStatus((prev) => prev ? { ...prev, authenticated: false, username: null, role: null, user_id: null } : null)
    }
    // Listen for login events (e.g. after invite password set)
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

  // Show loading state
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-fg-muted">Loading...</div>
      </div>
    )
  }

  // Show login if not authenticated
  if (!authStatus?.authenticated) {
    return <Login onLogin={handleLogin} error={loginError} />
  }

  return (
    <AuthProvider value={authContextValue}>
      <ProjectProvider>
        <Routes>
          {/* Project selection page */}
          <Route path="/" element={<ProjectSelection />} />

          {/* Settings (account, appearance, users) */}
          <Route path="/settings" element={<SettingsLayout />}>
            <Route index element={<Navigate to="account" replace />} />
            <Route path="account" element={<AccountSection />} />
            <Route path="appearance" element={<AppearanceSection />} />
            <Route path="users" element={<RequireAdmin><UsersSection /></RequireAdmin>} />
          </Route>

          {/* Project-scoped routes */}
          <Route
            path="/projects/:projectId/*"
            element={
              <ProjectLayout
                authStatus={authStatus}
                onLogout={handleLogout}
              />
            }
          />
        </Routes>
      </ProjectProvider>
    </AuthProvider>
  )
}

function ProjectLayout({
  authStatus,
  onLogout,
}: {
  authStatus: AuthStatus | null
  onLogout: () => void
}) {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const { selectedProject, setSelectedProjectId, isLoading } = useProject()
  const { isDark } = useTheme()
  const [showProjectDropdown, setShowProjectDropdown] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  const isAdmin = authStatus?.role === 'admin'
  const onSettings = location.pathname.startsWith(`/projects/${projectId}/settings`)

  const handleEnterSettings = () => {
    if (!onSettings) {
      setSettingsOrigin(location.pathname)
    }
    navigate(`/projects/${projectId}/settings`)
  }

  const handleBackFromSettings = () => {
    const from = popSettingsOrigin()
    navigate(from || `/projects/${projectId}/dashboard`)
  }

  // Sync project ID from URL to context
  useEffect(() => {
    if (projectId) {
      setSelectedProjectId(projectId)
    }
  }, [projectId, setSelectedProjectId])

  const handleSwitchProject = () => {
    setSelectedProjectId(null)
    navigate('/')
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-fg-muted">Loading project...</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className={`${sidebarCollapsed ? 'w-16' : 'w-64'} bg-surface border-r border-line flex flex-col transition-all duration-300 h-screen sticky top-0`}>
        <div className={`${sidebarCollapsed ? 'p-1 pt-3' : 'p-4'}`}>
          {/* Header with logo and collapse toggle */}
          <div className={`flex items-center ${sidebarCollapsed ? 'justify-center' : 'justify-between'}`}>
            {sidebarCollapsed ? (
              <button
                onClick={() => setSidebarCollapsed(false)}
                className="p-1 hover:opacity-80 transition-opacity"
                title="Expand sidebar"
              >
                <img src={isDark ? octoproxLogoOnlyDark : octoproxLogoOnly} alt="Octoprox" className="h-8" />
              </button>
            ) : (
              <>
                <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-10" />
                <button
                  onClick={() => setSidebarCollapsed(true)}
                  className="p-2 text-fg-muted hover:text-fg hover:bg-surface-raised rounded-lg transition-colors"
                  title="Collapse sidebar"
                >
                  <ChevronLeft className="w-5 h-5" />
                </button>
              </>
            )}
          </div>

          {/* Project selector */}
          {!sidebarCollapsed && (
            <div className="mt-3 relative">
              <button
                onClick={() => setShowProjectDropdown(!showProjectDropdown)}
                className="w-full flex items-center justify-between bg-surface-raised rounded-lg px-3 py-2 text-sm text-fg hover:bg-line transition-colors"
              >
                <span className="truncate">{selectedProject?.name || 'Select Project'}</span>
                <ChevronDown className="w-4 h-4 flex-shrink-0" />
              </button>
              {showProjectDropdown && (
                <div className="absolute top-full left-0 right-0 mt-1 bg-surface border border-line-strong rounded-lg shadow-lg z-10">
                  <button
                    onClick={handleSwitchProject}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-fg-muted hover:bg-surface-raised rounded-lg"
                  >
                    <FolderOpen className="w-4 h-4" />
                    Switch Project
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Collapsed project switcher */}
          {sidebarCollapsed && (
            <button
              onClick={handleSwitchProject}
              className="mt-3 w-full flex justify-center p-2 text-fg-muted hover:bg-surface-raised rounded-lg transition-colors"
              title="Switch Project"
            >
              <FolderOpen className="w-5 h-5" />
            </button>
          )}
        </div>

        <nav className="mt-4 flex-1">
          {onSettings ? (
            <>
              <button
                onClick={handleBackFromSettings}
                className={`w-full flex items-center gap-3 py-3 text-fg-muted hover:bg-surface-raised hover:text-fg transition-colors ${sidebarCollapsed ? 'justify-center px-2' : 'px-4'}`}
                title={sidebarCollapsed ? 'Back' : undefined}
              >
                <ArrowLeft className="w-5 h-5" />
                {!sidebarCollapsed && 'Back'}
              </button>
              <NavLink to={`/projects/${projectId}/settings/account`} icon={<UserIcon />} label="Account" collapsed={sidebarCollapsed} />
              <NavLink to={`/projects/${projectId}/settings/appearance`} icon={<Palette />} label="Appearance" collapsed={sidebarCollapsed} />
              {isAdmin && (
                <NavLink to={`/projects/${projectId}/settings/users`} icon={<UsersIcon />} label="Users" collapsed={sidebarCollapsed} />
              )}
            </>
          ) : (
            <>
              <NavLink to={`/projects/${projectId}/dashboard`} icon={<BarChart3 />} label="Dashboard" collapsed={sidebarCollapsed} />
              <NavLink to={`/projects/${projectId}/metrics`} icon={<LineChart />} label="Metrics" collapsed={sidebarCollapsed} />
              <NavLink to={`/projects/${projectId}/proxies`} icon={<Server />} label="Proxies" collapsed={sidebarCollapsed} />
              <NavLink to={`/projects/${projectId}/credentials`} icon={<Key />} label="Credentials" collapsed={sidebarCollapsed} />
              <NavLink to={`/projects/${projectId}/connectors`} icon={<Link2 />} label="Connectors" collapsed={sidebarCollapsed} />
              <NavLink to={`/projects/${projectId}/mitm-inspector`} icon={<Search />} label="MITM Inspector" collapsed={sidebarCollapsed} />
            </>
          )}
        </nav>

        {/* Bottom section: settings + user info + logout */}
        <div className="border-t border-line">
          <div className={`p-4 ${sidebarCollapsed ? 'flex flex-col items-center gap-2' : ''}`}>
            {sidebarCollapsed ? (
              <>
                <button
                  onClick={handleEnterSettings}
                  className="p-2 text-fg-muted hover:text-fg hover:bg-surface-raised rounded-lg transition-colors"
                  title="Settings"
                >
                  <SettingsIcon className="w-5 h-5" />
                </button>
                <button
                  onClick={onLogout}
                  className="p-2 text-fg-muted hover:text-fg hover:bg-surface-raised rounded-lg transition-colors"
                  title="Sign out"
                >
                  <LogOut className="w-5 h-5" />
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={handleEnterSettings}
                  className="flex items-center gap-2 text-sm text-fg-muted hover:text-fg transition-colors mb-2 w-full"
                  style={{ maxWidth: '100%' }}
                >
                  <SettingsIcon className="w-4 h-4 shrink-0" />
                  <span className="text-fg truncate min-w-0">{authStatus?.username}</span>
                  <span className="text-xs text-fg-subtle shrink-0">({authStatus?.role})</span>
                </button>
                <button
                  onClick={onLogout}
                  className="flex items-center gap-2 text-fg-muted hover:text-fg transition-colors text-sm"
                >
                  <LogOut className="w-4 h-4" />
                  Sign out
                </button>
              </>
            )}
          </div>
        </div>

      </aside>

      {/* Main content */}
      <main className="flex-1 p-8">
        <Routes>
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="metrics" element={<Metrics />} />
          <Route path="proxies" element={<ProxyList />} />
          <Route path="credentials" element={<CredentialsConfig />} />
          <Route path="connectors" element={<ConnectorConfig />} />
          <Route path="mitm-inspector" element={<MitmInspector />} />
          <Route path="settings" element={<Navigate to={`/projects/${projectId}/settings/account`} replace />} />
          <Route path="settings/account" element={<AccountSection />} />
          <Route path="settings/appearance" element={<AppearanceSection />} />
          <Route path="settings/users" element={<RequireAdmin><UsersSection /></RequireAdmin>} />
          <Route path="*" element={<Navigate to="dashboard" replace />} />
        </Routes>
      </main>

    </div>
  )
}

function NavLink({ to, icon, label, collapsed }: { to: string; icon: React.ReactNode; label: string; collapsed?: boolean }) {
  return (
    <Link
      to={to}
      className={`flex items-center gap-3 py-3 text-fg-muted hover:bg-surface-raised hover:text-fg transition-colors ${collapsed ? 'justify-center px-2' : 'px-4'}`}
      title={collapsed ? label : undefined}
    >
      {icon}
      {!collapsed && label}
    </Link>
  )
}

export default App
