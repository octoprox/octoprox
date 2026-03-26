// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Activity, Server, CheckCircle, XCircle, Timer, Settings, ArrowUpCircle, ArrowDownCircle, TrendingUp, Loader, Power } from 'lucide-react'
import { fetchProjectMetrics, fetchProjectScalingMetrics, updateProject, ProjectUpdate, ScalingMetrics } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import ProjectModal from './ProjectModal'
import { formatBytes } from '../utils/format'
import { Card, Badge, Button } from './ui'

export default function Dashboard() {
  const queryClient = useQueryClient()
  const { selectedProjectId, selectedProject, refreshProject } = useProject()
  const [showEditModal, setShowEditModal] = useState(false)

  const { data: metrics } = useQuery({
    queryKey: ['metrics', selectedProjectId],
    queryFn: () => fetchProjectMetrics(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const { data: scalingMetrics } = useQuery({
    queryKey: ['scaling-metrics', selectedProjectId],
    queryFn: () => fetchProjectScalingMetrics(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10000, // Refresh every 10 seconds
  })

  const updateMutation = useMutation({
    mutationFn: (data: ProjectUpdate) => updateProject(selectedProjectId!, data),
    onSuccess: async () => {
      queryClient.invalidateQueries({ queryKey: ['metrics', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      await refreshProject()
      setShowEditModal(false)
    },
  })

  const strategyMutation = useMutation({
    mutationFn: (strategy: string) => updateProject(selectedProjectId!, { routing_strategy: strategy }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['metrics', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })

  const pool = metrics?.pool
  const strategy = metrics?.strategy

  return (
    <div>
      {/* Header with Edit Button */}
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">Dashboard</h1>
        {selectedProject && (
          <Button
            variant="ghost"
            onClick={() => {
              updateMutation.reset()
              setShowEditModal(true)
            }}
          >
            <Settings className="w-5 h-5" />
            Edit Project
          </Button>
        )}
      </div>

      {/* Pool Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-6 mb-8">
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
          title="Quarantined"
          value={pool?.quarantined_proxies ?? 0}
          icon={<Timer className="w-8 h-8 text-orange-500" />}
        />
        <StatCard
          title="Unhealthy"
          value={pool?.unhealthy_proxies ?? 0}
          icon={<XCircle className="w-8 h-8 text-red-500" />}
        />
        <StatCard
          title="Avg Latency"
          value={`${pool?.avg_latency_ms?.toFixed(0) ?? 0}ms`}
          icon={<Activity className="w-8 h-8 text-purple-500" />}
        />
      </div>

      {/* Request Statistics */}
      <Card className="p-6 mb-8">
        <h2 className="text-xl font-semibold mb-4">Request Statistics</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-6">
          <MetricItem label="Total Requests" value={pool?.total_requests ?? 0} />
          <MetricItem label="Successes" value={pool?.total_successes ?? 0} color="text-green-600" />
          <MetricItem label="Failures" value={pool?.total_failures ?? 0} color="text-red-600" />
          <MetricItem
            label="Success Rate"
            value={`${pool?.overall_success_rate?.toFixed(1) ?? 0}%`}
            color="text-blue-600"
          />
          <MetricItem
            label="Bytes Sent"
            value={formatBytes(pool?.total_bytes_sent ?? 0)}
            color="text-orange-600"
            icon={<ArrowUpCircle className="w-4 h-4 text-orange-500" />}
          />
          <MetricItem
            label="Bytes Received"
            value={formatBytes(pool?.total_bytes_received ?? 0)}
            color="text-teal-600"
            icon={<ArrowDownCircle className="w-4 h-4 text-teal-500" />}
          />
        </div>
      </Card>

      {/* Auto-Scaling Status */}
      {scalingMetrics && (
        <Card className="p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">Auto-Scaling Status</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
            <div>
              <p className="text-gray-500 dark:text-gray-400 text-sm mb-1">Demand Level</p>
              <DemandBadge level={scalingMetrics.demand_level} />
            </div>
            <MetricItem
              label="Requests/min"
              value={scalingMetrics.requests_per_minute.toFixed(1)}
              icon={<TrendingUp className="w-4 h-4 text-blue-500" />}
            />
            <MetricItem
              label="Rate/Proxy"
              value={scalingMetrics.rate_per_proxy.toFixed(1)}
            />
            <MetricItem
              label="Instances"
              value={`${scalingMetrics.current_instances} / ${scalingMetrics.max_instances}`}
              color="text-blue-600"
            />
          </div>
          {(scalingMetrics.draining_instances > 0 || scalingMetrics.terminating_instances > 0) && (
            <div className="mt-4 pt-4 border-t border-gray-100 dark:border-gray-700 flex gap-6">
              {scalingMetrics.draining_instances > 0 && (
                <div className="flex items-center gap-2">
                  <Loader className="w-4 h-4 text-orange-500 animate-spin" />
                  <span className="text-sm text-orange-600">
                    {scalingMetrics.draining_instances} draining
                  </span>
                </div>
              )}
              {scalingMetrics.terminating_instances > 0 && (
                <div className="flex items-center gap-2">
                  <Power className="w-4 h-4 text-purple-500" />
                  <span className="text-sm text-purple-600">
                    {scalingMetrics.terminating_instances} terminating
                  </span>
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Routing Strategy */}
      <Card className="p-6">
        <h2 className="text-xl font-semibold mb-4">Routing Strategy</h2>
        <div className="flex flex-wrap gap-3">
          {strategy?.available_strategies.map((s) => (
            <Button
              key={s}
              variant={s === strategy.current_strategy ? 'primary' : 'secondary'}
              onClick={() => strategyMutation.mutate(s)}
              disabled={strategyMutation.isPending}
            >
              {formatStrategy(s)}
            </Button>
          ))}
        </div>
        <p className="mt-4 text-gray-500 dark:text-gray-400 text-sm">
          Current strategy: <strong>{formatStrategy(strategy?.current_strategy ?? '')}</strong>
        </p>
      </Card>

      {/* Edit Project Modal */}
      {showEditModal && selectedProject && (
        <ProjectModal
          project={selectedProject}
          onClose={() => setShowEditModal(false)}
          onSave={(data) => updateMutation.mutate(data)}
          isLoading={updateMutation.isPending}
          error={updateMutation.error?.message}
        />
      )}
    </div>
  )
}

function StatCard({ title, value, icon }: { title: string; value: string | number; icon: React.ReactNode }) {
  return (
    <Card className="p-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-gray-500 dark:text-gray-400 text-sm">{title}</p>
          <p className="text-3xl font-bold mt-1">{value}</p>
        </div>
        {icon}
      </div>
    </Card>
  )
}

function MetricItem({
  label,
  value,
  color = 'text-gray-900 dark:text-gray-100',
  icon,
}: {
  label: string
  value: string | number
  color?: string
  icon?: React.ReactNode
}) {
  return (
    <div>
      <div className="flex items-center gap-1">
        {icon}
        <p className="text-gray-500 dark:text-gray-400 text-sm">{label}</p>
      </div>
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

const DEMAND_COLORS: Record<string, 'green' | 'yellow' | 'red'> = {
  LOW: 'green',
  MEDIUM: 'yellow',
  HIGH: 'red',
}

function DemandBadge({ level }: { level: ScalingMetrics['demand_level'] }) {
  return (
    <Badge color={DEMAND_COLORS[level] ?? 'green'} className="px-3 text-sm font-semibold">
      {level}
    </Badge>
  )
}
