import { useQuery } from '@tanstack/react-query'
import { Activity, Server, CheckCircle, XCircle } from 'lucide-react'
import { fetchProjectMetrics, fetchProjectProxies } from '../api/client'
import { useProject } from '../contexts/ProjectContext'

export default function Dashboard() {
  const { selectedProjectId, selectedProject } = useProject()

  const { data: metrics } = useQuery({
    queryKey: ['metrics', selectedProjectId],
    queryFn: () => fetchProjectMetrics(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const { data: proxies } = useQuery({
    queryKey: ['proxies', selectedProjectId],
    queryFn: () => fetchProjectProxies(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const pool = metrics?.pool

  return (
    <div>
      <h1 className="text-3xl font-bold mb-2">Dashboard</h1>
      {selectedProject && (
        <p className="text-gray-500 mb-6">Project: {selectedProject.name}</p>
      )}

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <StatCard
          title="Total Proxies"
          value={pool?.total_proxies ?? 0}
          icon={<Server className="w-8 h-8 text-blue-500" />}
        />
        <StatCard
          title="Healthy"
          value={pool?.healthy_proxies ?? 0}
          icon={<CheckCircle className="w-8 h-8 text-green-500" />}
        />
        <StatCard
          title="Unhealthy"
          value={pool?.unhealthy_proxies ?? 0}
          icon={<XCircle className="w-8 h-8 text-red-500" />}
        />
        <StatCard
          title="Success Rate"
          value={`${pool?.overall_success_rate ?? 0}%`}
          icon={<Activity className="w-8 h-8 text-purple-500" />}
        />
      </div>

      {/* Strategy Info */}
      <div className="bg-white rounded-lg shadow p-6 mb-8">
        <h2 className="text-xl font-semibold mb-4">Routing Strategy</h2>
        <div className="flex items-center gap-4">
          <span className="text-gray-600">Current:</span>
          <span className="px-3 py-1 bg-blue-100 text-blue-800 rounded-full font-medium">
            {metrics?.strategy.current_strategy ?? 'N/A'}
          </span>
        </div>
      </div>

      {/* Recent Proxies */}
      <div className="bg-white rounded-lg shadow p-6">
        <h2 className="text-xl font-semibold mb-4">Proxy Pool</h2>
        {proxies?.proxies.length === 0 ? (
          <p className="text-gray-500">No proxies configured. Add proxies to get started.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-500 border-b">
                  <th className="pb-3">Host</th>
                  <th className="pb-3">Status</th>
                  <th className="pb-3">Requests</th>
                  <th className="pb-3">Success Rate</th>
                  <th className="pb-3">Latency</th>
                </tr>
              </thead>
              <tbody>
                {proxies?.proxies.slice(0, 5).map((proxy) => (
                  <tr key={proxy.id} className="border-b last:border-0">
                    <td className="py-3">{proxy.host}:{proxy.port}</td>
                    <td className="py-3">
                      <StatusBadge status={proxy.status} />
                    </td>
                    <td className="py-3">{proxy.request_count}</td>
                    <td className="py-3">{proxy.success_rate.toFixed(1)}%</td>
                    <td className="py-3">{proxy.avg_latency_ms.toFixed(0)}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function StatCard({ title, value, icon }: { title: string; value: string | number; icon: React.ReactNode }) {
  return (
    <div className="bg-white rounded-lg shadow p-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-gray-500 text-sm">{title}</p>
          <p className="text-3xl font-bold mt-1">{value}</p>
        </div>
        {icon}
      </div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    healthy: 'bg-green-100 text-green-800',
    degraded: 'bg-yellow-100 text-yellow-800',
    unhealthy: 'bg-red-100 text-red-800',
    unknown: 'bg-gray-100 text-gray-800',
  }
  return (
    <span className={`px-2 py-1 rounded-full text-xs font-medium ${colors[status] ?? colors.unknown}`}>
      {status}
    </span>
  )
}

