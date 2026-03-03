// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo } from 'react'
import { BrowserRouter, Routes, Route, Link, Navigate, useParams, useNavigate } from 'react-router-dom'
import { Server, BarChart3, LineChart, LogOut, FolderOpen, ChevronDown, ChevronLeft, Key, Link2, Moon, Sun, Search, Users, User } from 'lucide-react'
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
import UsersPage from './components/UsersPage'
import ProfileModal from './components/ProfileModal'
import { ProjectProvider, useProject } from './contexts/ProjectContext'
import { AuthProvider, AuthContextValue } from './contexts/AuthContext'
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

  const refreshAuth = async () => {
    try {
      const status = await checkAuthStatus()
      setAuthStatus(status)
    } catch {
      setAuthStatus({ authenticated: false, username: null, role: null, user_id: null })
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
      <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="text-gray-500 dark:text-gray-400">Loading...</div>
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

          {/* Users management (top-level, admin only) */}
          <Route path="/users" element={<UsersPage />} />

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
  const { selectedProject, setSelectedProjectId, isLoading } = useProject()
  const { theme, toggleTheme } = useTheme()
  const [showProjectDropdown, setShowProjectDropdown] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [showProfileModal, setShowProfileModal] = useState(false)

  const isAdmin = authStatus?.role === 'admin'

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
      <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="text-gray-500 dark:text-gray-400">Loading project...</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className={`${sidebarCollapsed ? 'w-16' : 'w-64'} bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 flex flex-col transition-all duration-300 h-screen sticky top-0`}>
        <div className={`${sidebarCollapsed ? 'p-1 pt-3' : 'p-4'}`}>
          {/* Header with logo and collapse toggle */}
          <div className={`flex items-center ${sidebarCollapsed ? 'justify-center' : 'justify-between'}`}>
            {sidebarCollapsed ? (
              <button
                onClick={() => setSidebarCollapsed(false)}
                className="p-1 hover:opacity-80 transition-opacity"
                title="Expand sidebar"
              >
                <img src={theme === 'dark' ? octoproxLogoOnlyDark : octoproxLogoOnly} alt="Octoprox" className="h-8" />
              </button>
            ) : (
              <>
                <img src={theme === 'dark' ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-10" />
                <button
                  onClick={() => setSidebarCollapsed(true)}
                  className="p-2 text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
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
                className="w-full flex items-center justify-between bg-gray-100 dark:bg-gray-700 rounded-lg px-3 py-2 text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
              >
                <span className="truncate">{selectedProject?.name || 'Select Project'}</span>
                <ChevronDown className="w-4 h-4 flex-shrink-0" />
              </button>
              {showProjectDropdown && (
                <div className="absolute top-full left-0 right-0 mt-1 bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg z-10">
                  <button
                    onClick={handleSwitchProject}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-600 rounded-lg"
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
              className="mt-3 w-full flex justify-center p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              title="Switch Project"
            >
              <FolderOpen className="w-5 h-5" />
            </button>
          )}
        </div>

        <nav className="mt-4 flex-1">
          <NavLink to={`/projects/${projectId}/dashboard`} icon={<BarChart3 />} label="Dashboard" collapsed={sidebarCollapsed} />
          <NavLink to={`/projects/${projectId}/metrics`} icon={<LineChart />} label="Metrics" collapsed={sidebarCollapsed} />
          <NavLink to={`/projects/${projectId}/proxies`} icon={<Server />} label="Proxies" collapsed={sidebarCollapsed} />
          <NavLink to={`/projects/${projectId}/credentials`} icon={<Key />} label="Credentials" collapsed={sidebarCollapsed} />
          <NavLink to={`/projects/${projectId}/connectors`} icon={<Link2 />} label="Connectors" collapsed={sidebarCollapsed} />
          <NavLink to={`/projects/${projectId}/mitm-inspector`} icon={<Search />} label="MITM Inspector" collapsed={sidebarCollapsed} />
          {isAdmin && (
            <NavLink to={`/projects/${projectId}/users`} icon={<Users />} label="Users" collapsed={sidebarCollapsed} />
          )}
        </nav>

        {/* Bottom section: theme toggle + user info */}
        <div className="border-t border-gray-200 dark:border-gray-700">
          {/* Theme toggle */}
          <div className={`p-4 ${sidebarCollapsed ? 'flex justify-center' : ''}`}>
            {sidebarCollapsed ? (
              <button
                onClick={toggleTheme}
                className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              >
                {theme === 'dark' ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
              </button>
            ) : (
              <button
                onClick={toggleTheme}
                className="flex items-center gap-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 transition-colors text-sm"
              >
                {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
                {theme === 'dark' ? 'Light mode' : 'Dark mode'}
              </button>
            )}
          </div>

          {/* User info and logout */}
          <div className={`p-4 border-t border-gray-200 dark:border-gray-700 ${sidebarCollapsed ? 'flex flex-col items-center gap-2' : ''}`}>
            {sidebarCollapsed ? (
              <>
                <button
                  onClick={() => setShowProfileModal(true)}
                  className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  title="Profile"
                >
                  <User className="w-5 h-5" />
                </button>
                <button
                  onClick={onLogout}
                  className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  title="Sign out"
                >
                  <LogOut className="w-5 h-5" />
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={() => setShowProfileModal(true)}
                  className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 transition-colors mb-2 w-full"
                  style={{ maxWidth: '100%' }}
                >
                  <User className="w-4 h-4 shrink-0" />
                  <span className="text-gray-900 dark:text-gray-100 truncate min-w-0">{authStatus?.username}</span>
                  <span className="text-xs text-gray-400 dark:text-gray-500 shrink-0">({authStatus?.role})</span>
                </button>
                <button
                  onClick={onLogout}
                  className="flex items-center gap-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 transition-colors text-sm"
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
          <Route path="users" element={<UsersPage />} />
          <Route path="*" element={<Navigate to="dashboard" replace />} />
        </Routes>
      </main>

      {/* Profile Modal */}
      {showProfileModal && authStatus && (
        <ProfileModal
          username={authStatus.username || ''}
          email=""
          onClose={() => setShowProfileModal(false)}
          onSuccess={() => setShowProfileModal(false)}
        />
      )}
    </div>
  )
}

function NavLink({ to, icon, label, collapsed }: { to: string; icon: React.ReactNode; label: string; collapsed?: boolean }) {
  return (
    <Link
      to={to}
      className={`flex items-center gap-3 py-3 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-gray-100 transition-colors ${collapsed ? 'justify-center px-2' : 'px-4'}`}
      title={collapsed ? label : undefined}
    >
      {icon}
      {!collapsed && label}
    </Link>
  )
}

export default App
