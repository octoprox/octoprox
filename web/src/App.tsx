// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Link, Navigate, useParams, useNavigate } from 'react-router-dom'
import { Server, BarChart3, LineChart, LogOut, FolderOpen, ChevronDown, ChevronLeft, Key, Link2, Moon, Sun } from 'lucide-react'
import Dashboard from './components/Dashboard'
import octoproxLogo from './assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from './assets/logos/octoprox_horizontal_dark.svg'
import octoproxLogoOnly from './assets/logos/octoprox_logo_only.svg'
import octoproxLogoOnlyDark from './assets/logos/octoprox_logo_only_dark.svg'
import ProxyList from './components/ProxyList'
import CredentialsConfig from './components/CredentialsConfig'
import ConnectorConfig from './components/ConnectorConfig'
import Metrics from './components/Metrics'
import Login from './components/Login'
import ProjectSelection from './components/ProjectSelection'
import { ProjectProvider, useProject } from './contexts/ProjectContext'
import { useTheme } from './contexts/ThemeContext'
import { checkAuthStatus, login, logout, AuthStatus } from './api/client'

function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loginError, setLoginError] = useState<string | null>(null)

  // Check auth status on mount
  useEffect(() => {
    const checkAuth = async () => {
      try {
        const status = await checkAuthStatus()
        setAuthStatus(status)
      } catch (error) {
        // If we can't reach the server, assume auth is disabled
        setAuthStatus({ enabled: false, authenticated: false, username: null })
      } finally {
        setIsLoading(false)
      }
    }
    checkAuth()

    // Listen for logout events
    const handleLogout = () => {
      setAuthStatus((prev) => prev ? { ...prev, authenticated: false, username: null } : null)
    }
    window.addEventListener('auth:logout', handleLogout)
    return () => window.removeEventListener('auth:logout', handleLogout)
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
    setAuthStatus((prev) => prev ? { ...prev, authenticated: false, username: null } : null)
  }

  // Show loading state
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="text-gray-500 dark:text-gray-400">Loading...</div>
      </div>
    )
  }

  // Show login if auth is enabled and not authenticated
  if (authStatus?.enabled && !authStatus.authenticated) {
    return <Login onLogin={handleLogin} error={loginError} />
  }

  return (
    <ProjectProvider>
      <BrowserRouter>
        <Routes>
          {/* Project selection page */}
          <Route path="/" element={<ProjectSelection />} />

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
      </BrowserRouter>
    </ProjectProvider>
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
          {authStatus?.enabled && authStatus.authenticated && (
            <div className={`p-4 border-t border-gray-200 dark:border-gray-700 ${sidebarCollapsed ? 'flex justify-center' : ''}`}>
              {sidebarCollapsed ? (
                <button
                  onClick={onLogout}
                  className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  title="Sign out"
                >
                  <LogOut className="w-5 h-5" />
                </button>
              ) : (
                <>
                  <div className="text-sm text-gray-500 dark:text-gray-400 mb-2">
                    Signed in as <span className="text-gray-900 dark:text-gray-100">{authStatus.username}</span>
                  </div>
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
          )}
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
      className={`flex items-center gap-3 py-3 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-gray-100 transition-colors ${collapsed ? 'justify-center px-2' : 'px-4'}`}
      title={collapsed ? label : undefined}
    >
      {icon}
      {!collapsed && label}
    </Link>
  )
}

export default App
