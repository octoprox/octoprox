import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { fetchProxies, createProxy, deleteProxy } from '../api/client'

export default function ProxyList() {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [formData, setFormData] = useState({ host: '', port: '', protocol: 'http' })

  const { data, isLoading } = useQuery({
    queryKey: ['proxies'],
    queryFn: fetchProxies,
  })

  const createMutation = useMutation({
    mutationFn: createProxy,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proxies'] })
      setShowForm(false)
      setFormData({ host: '', port: '', protocol: 'http' })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteProxy,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proxies'] })
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    createMutation.mutate({
      host: formData.host,
      port: parseInt(formData.port),
      protocol: formData.protocol,
    })
  }

  if (isLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Proxies</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          <Plus className="w-5 h-5" />
          Add Proxy
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 mb-8">
          <div className="grid grid-cols-3 gap-4">
            <input
              type="text"
              placeholder="Host"
              value={formData.host}
              onChange={(e) => setFormData({ ...formData, host: e.target.value })}
              className="px-4 py-2 border rounded-lg"
              required
            />
            <input
              type="number"
              placeholder="Port"
              value={formData.port}
              onChange={(e) => setFormData({ ...formData, port: e.target.value })}
              className="px-4 py-2 border rounded-lg"
              required
            />
            <select
              value={formData.protocol}
              onChange={(e) => setFormData({ ...formData, protocol: e.target.value })}
              className="px-4 py-2 border rounded-lg"
            >
              <option value="http">HTTP</option>
              <option value="https">HTTPS</option>
              <option value="socks5">SOCKS5</option>
            </select>
          </div>
          <button
            type="submit"
            className="mt-4 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
          >
            Create Proxy
          </button>
        </form>
      )}

      <div className="bg-white rounded-lg shadow">
        <table className="w-full">
          <thead>
            <tr className="text-left text-gray-500 border-b">
              <th className="p-4">Host</th>
              <th className="p-4">Protocol</th>
              <th className="p-4">Status</th>
              <th className="p-4">Requests</th>
              <th className="p-4">Success Rate</th>
              <th className="p-4">Latency</th>
              <th className="p-4">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data?.proxies.map((proxy) => (
              <tr key={proxy.id} className="border-b last:border-0 hover:bg-gray-50">
                <td className="p-4 font-mono">{proxy.host}:{proxy.port}</td>
                <td className="p-4 uppercase text-sm">{proxy.protocol}</td>
                <td className="p-4">
                  <StatusBadge status={proxy.status} />
                </td>
                <td className="p-4">{proxy.request_count}</td>
                <td className="p-4">{proxy.success_rate.toFixed(1)}%</td>
                <td className="p-4">{proxy.avg_latency_ms.toFixed(0)}ms</td>
                <td className="p-4">
                  <button
                    onClick={() => deleteMutation.mutate(proxy.id)}
                    className="text-red-600 hover:text-red-800"
                  >
                    <Trash2 className="w-5 h-5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data?.proxies.length === 0 && (
          <p className="p-8 text-center text-gray-500">No proxies configured.</p>
        )}
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

