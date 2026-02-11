import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Link } from 'react-router-dom'
import { Activity, Server, Settings, BarChart3, LogOut } from 'lucide-react'
import Dashboard from './components/Dashboard'
import ProxyList from './components/ProxyList'
import SourceConfig from './components/SourceConfig'
import Metrics from './components/Metrics'
import Login from './components/Login'
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
      <div className="min-h-screen flex items-center justify-center bg-gray-100">
        <div className="text-gray-500">Loading...</div>
      </div>
    )
  }

  // Show login if auth is enabled and not authenticated
  if (authStatus?.enabled && !authStatus.authenticated) {
    return <Login onLogin={handleLogin} error={loginError} />
  }

  return (
    <BrowserRouter>
      <div className="min-h-screen flex">
        {/* Sidebar */}
        <aside className="w-64 bg-gray-900 text-white flex flex-col">
          <div className="p-4">
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Activity className="w-8 h-8 text-blue-400" />
              Octoprox
            </h1>
            <p className="text-gray-400 text-sm mt-1">Proxy Manager</p>
          </div>

          <nav className="mt-8 flex-1">
            <NavLink to="/" icon={<BarChart3 />} label="Dashboard" />
            <NavLink to="/proxies" icon={<Server />} label="Proxies" />
            <NavLink to="/sources" icon={<Settings />} label="Sources" />
            <NavLink to="/metrics" icon={<Activity />} label="Metrics" />
          </nav>

          {/* User info and logout */}
          {authStatus?.enabled && authStatus.authenticated && (
            <div className="p-4 border-t border-gray-800">
              <div className="text-sm text-gray-400 mb-2">
                Signed in as <span className="text-white">{authStatus.username}</span>
              </div>
              <button
                onClick={handleLogout}
                className="flex items-center gap-2 text-gray-400 hover:text-white transition-colors text-sm"
              >
                <LogOut className="w-4 h-4" />
                Sign out
              </button>
            </div>
          )}
        </aside>

        {/* Main content */}
        <main className="flex-1 p-8">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/proxies" element={<ProxyList />} />
            <Route path="/sources" element={<SourceConfig />} />
            <Route path="/metrics" element={<Metrics />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}

function NavLink({ to, icon, label }: { to: string; icon: React.ReactNode; label: string }) {
  return (
    <Link
      to={to}
      className="flex items-center gap-3 px-4 py-3 text-gray-300 hover:bg-gray-800 hover:text-white transition-colors"
    >
      {icon}
      {label}
    </Link>
  )
}

export default App

