import { useQuery } from '@tanstack/react-query'
import { Database, RefreshCw } from 'lucide-react'
import { fetchSources } from '../api/client'

export default function SourceConfig() {
  const { data, isLoading } = useQuery({
    queryKey: ['sources'],
    queryFn: fetchSources,
  })

  if (isLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Proxy Sources</h1>
        <button className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          <Database className="w-5 h-5" />
          Add Source
        </button>
      </div>

      <div className="grid gap-6">
        {data?.sources.map((source) => (
          <div key={source.id} className="bg-white rounded-lg shadow p-6">
            <div className="flex justify-between items-start">
              <div>
                <h3 className="text-xl font-semibold">{source.name}</h3>
                <p className="text-gray-500 mt-1">Type: {source.type}</p>
              </div>
              <div className="flex items-center gap-4">
                <span
                  className={`px-3 py-1 rounded-full text-sm font-medium ${
                    source.enabled
                      ? 'bg-green-100 text-green-800'
                      : 'bg-gray-100 text-gray-800'
                  }`}
                >
                  {source.enabled ? 'Enabled' : 'Disabled'}
                </span>
                <button className="p-2 text-gray-500 hover:text-blue-600">
                  <RefreshCw className="w-5 h-5" />
                </button>
              </div>
            </div>

            <div className="mt-4 grid grid-cols-3 gap-4 text-sm">
              <div>
                <span className="text-gray-500">Proxies:</span>
                <span className="ml-2 font-medium">{source.proxy_count}</span>
              </div>
              <div>
                <span className="text-gray-500">Refresh Interval:</span>
                <span className="ml-2 font-medium">{source.refresh_interval_seconds}s</span>
              </div>
              <div>
                <span className="text-gray-500">Last Refresh:</span>
                <span className="ml-2 font-medium">
                  {source.last_refresh
                    ? new Date(source.last_refresh).toLocaleString()
                    : 'Never'}
                </span>
              </div>
            </div>
          </div>
        ))}

        {data?.sources.length === 0 && (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <Database className="w-12 h-12 text-gray-400 mx-auto mb-4" />
            <h3 className="text-lg font-medium text-gray-900">No sources configured</h3>
            <p className="text-gray-500 mt-2">
              Add a proxy source to start populating your proxy pool.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

