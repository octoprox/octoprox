import { BrowserRouter, Routes, Route, Link } from 'react-router-dom'
import { Activity, Server, Settings, BarChart3 } from 'lucide-react'
import Dashboard from './components/Dashboard'
import ProxyList from './components/ProxyList'
import SourceConfig from './components/SourceConfig'
import Metrics from './components/Metrics'

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex">
        {/* Sidebar */}
        <aside className="w-64 bg-gray-900 text-white">
          <div className="p-4">
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Activity className="w-8 h-8 text-blue-400" />
              Octoprox
            </h1>
            <p className="text-gray-400 text-sm mt-1">Proxy Manager</p>
          </div>
          
          <nav className="mt-8">
            <NavLink to="/" icon={<BarChart3 />} label="Dashboard" />
            <NavLink to="/proxies" icon={<Server />} label="Proxies" />
            <NavLink to="/sources" icon={<Settings />} label="Sources" />
            <NavLink to="/metrics" icon={<Activity />} label="Metrics" />
          </nav>
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

