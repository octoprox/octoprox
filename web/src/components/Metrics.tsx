import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchProjectMetrics, setStrategy } from '../api/client'
import { useProject } from '../contexts/ProjectContext'

export default function Metrics() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()

  const { data, isLoading } = useQuery({
    queryKey: ['metrics', selectedProjectId],
    queryFn: () => fetchProjectMetrics(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const strategyMutation = useMutation({
    mutationFn: setStrategy,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['metrics', selectedProjectId] })
    },
  })

  if (isLoading) return <div>Loading...</div>

  const pool = data?.pool
  const strategy = data?.strategy

  return (
    <div>
      <h1 className="text-3xl font-bold mb-8">Metrics & Configuration</h1>

      {/* Pool Metrics */}
      <div className="bg-white rounded-lg shadow p-6 mb-8">
        <h2 className="text-xl font-semibold mb-4">Pool Metrics</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
          <MetricItem label="Total Proxies" value={pool?.total_proxies ?? 0} />
          <MetricItem label="Healthy" value={pool?.healthy_proxies ?? 0} color="text-green-600" />
          <MetricItem label="Unhealthy" value={pool?.unhealthy_proxies ?? 0} color="text-red-600" />
          <MetricItem label="Avg Latency" value={`${pool?.avg_latency_ms?.toFixed(0) ?? 0}ms`} />
        </div>
      </div>

      {/* Request Stats */}
      <div className="bg-white rounded-lg shadow p-6 mb-8">
        <h2 className="text-xl font-semibold mb-4">Request Statistics</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
          <MetricItem label="Total Requests" value={pool?.total_requests ?? 0} />
          <MetricItem label="Successes" value={pool?.total_successes ?? 0} color="text-green-600" />
          <MetricItem label="Failures" value={pool?.total_failures ?? 0} color="text-red-600" />
          <MetricItem
            label="Success Rate"
            value={`${pool?.overall_success_rate?.toFixed(1) ?? 0}%`}
            color="text-blue-600"
          />
        </div>
      </div>

      {/* Routing Strategy */}
      <div className="bg-white rounded-lg shadow p-6">
        <h2 className="text-xl font-semibold mb-4">Routing Strategy</h2>
        <div className="flex flex-wrap gap-3">
          {strategy?.available_strategies.map((s) => (
            <button
              key={s}
              onClick={() => strategyMutation.mutate(s)}
              className={`px-4 py-2 rounded-lg font-medium transition-colors ${
                s === strategy.current_strategy
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              {formatStrategy(s)}
            </button>
          ))}
        </div>
        <p className="mt-4 text-gray-500 text-sm">
          Current strategy: <strong>{formatStrategy(strategy?.current_strategy ?? '')}</strong>
        </p>
      </div>
    </div>
  )
}

function MetricItem({
  label,
  value,
  color = 'text-gray-900',
}: {
  label: string
  value: string | number
  color?: string
}) {
  return (
    <div>
      <p className="text-gray-500 text-sm">{label}</p>
      <p className={`text-2xl font-bold ${color}`}>{value}</p>
    </div>
  )
}

function formatStrategy(strategy: string): string {
  return strategy
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

