// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { ArrowUpCircle, ArrowDownCircle } from 'lucide-react'
import { fetchProjectMetricsHistory, MetricsSnapshot } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import { formatBytes } from '../utils/format'
import { Card, Button } from './ui'

const TIME_RANGES = ['1h', '6h', '24h', '7d', '30d'] as const
type TimeRange = (typeof TIME_RANGES)[number]

// Gap thresholds per range: 2x the expected interval between points.
// Raw ranges flush every 60s → 120s threshold.
// 7d aggregates hourly → 2h threshold. 30d aggregates every 6h → 12h threshold.
const GAP_THRESHOLD_MS: Record<TimeRange, number> = {
  '1h':  120_000,
  '6h':  120_000,
  '24h': 120_000,
  '7d':  2 * 3600_000,
  '30d': 2 * 6 * 3600_000,
}

type ChartPoint = {
  time: number
  request_count: number | null
  success_count: number | null
  failure_count: number | null
  avg_latency_ms: number | null
  bytes_sent: number | null
  bytes_received: number | null
}

function formatTickByRange(epoch: number, range: TimeRange): string {
  const d = new Date(epoch)
  if (range === '7d' || range === '30d') {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/**
 * Convert snapshots to chart points, inserting null-valued gap markers
 * wherever consecutive points are more than GAP_THRESHOLD_MS apart.
 * This causes recharts Area (connectNulls=false) to break the line.
 */
function buildChartData(snapshots: MetricsSnapshot[], range: TimeRange): ChartPoint[] {
  if (snapshots.length === 0) return []
  const threshold = GAP_THRESHOLD_MS[range]
  const points: ChartPoint[] = []
  for (let i = 0; i < snapshots.length; i++) {
    const s = snapshots[i]
    const epoch = new Date(s.timestamp).getTime()
    if (i > 0) {
      const prevEpoch = new Date(snapshots[i - 1].timestamp).getTime()
      if (epoch - prevEpoch > threshold) {
        // Insert a null point just after the previous point to break the line
        points.push({
          time: prevEpoch + 1,
          request_count: null, success_count: null, failure_count: null,
          avg_latency_ms: null, bytes_sent: null, bytes_received: null,
        })
      }
    }
    points.push({
      time: epoch,
      request_count: s.request_count,
      success_count: s.success_count,
      failure_count: s.failure_count,
      avg_latency_ms: s.avg_latency_ms,
      bytes_sent: s.bytes_sent,
      bytes_received: s.bytes_received,
    })
  }
  return points
}

function computeTotals(snapshots: MetricsSnapshot[]) {
  let requests = 0, successes = 0, failures = 0, bytesSent = 0, bytesReceived = 0
  for (const s of snapshots) {
    requests += s.request_count
    successes += s.success_count
    failures += s.failure_count
    bytesSent += s.bytes_sent
    bytesReceived += s.bytes_received
  }
  const successRate = requests > 0 ? (successes / requests) * 100 : 0
  return { requests, successes, failures, successRate, bytesSent, bytesReceived }
}

export default function Metrics() {
  const { selectedProjectId } = useProject()
  const { theme } = useTheme()
  const [range, setRange] = useState<TimeRange>('24h')

  const { data } = useQuery({
    queryKey: ['metrics-history', selectedProjectId, range],
    queryFn: () => fetchProjectMetricsHistory(selectedProjectId!, range),
    enabled: !!selectedProjectId,
    refetchInterval: 60000,
  })

  const snapshots = data?.snapshots ?? []
  const totals = useMemo(() => computeTotals(snapshots), [snapshots])

  const chartData = useMemo(() => buildChartData(snapshots, range), [snapshots, range])

  const gridColor = theme === 'dark' ? '#374151' : '#e5e7eb'
  const tickColor = theme === 'dark' ? '#9ca3af' : undefined

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">Metrics</h1>
      </div>
      {/* Time range selector */}
      <div className="flex gap-2 mb-6">
        {TIME_RANGES.map((r) => (
          <Button
            key={r}
            variant={r === range ? 'primary' : 'secondary'}
            onClick={() => setRange(r)}
          >
            {r}
          </Button>
        ))}
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
        <StatCard title="Total Requests" value={totals.requests.toLocaleString()} />
        <StatCard title="Successes" value={totals.successes.toLocaleString()} color="text-green-600" />
        <StatCard title="Failures" value={totals.failures.toLocaleString()} color="text-red-600" />
        <StatCard title="Success Rate" value={`${totals.successRate.toFixed(1)}%`} color="text-blue-600" />
        <StatCard
          title="Bytes Sent"
          value={formatBytes(totals.bytesSent)}
          color="text-orange-600"
          icon={<ArrowUpCircle className="w-5 h-5 text-orange-500" />}
        />
        <StatCard
          title="Bytes Received"
          value={formatBytes(totals.bytesReceived)}
          color="text-teal-600"
          icon={<ArrowDownCircle className="w-5 h-5 text-teal-500" />}
        />
      </div>

      {/* Charts grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Requests Over Time */}
        <Card className="p-6 min-w-0">
          <h2 className="text-xl font-semibold mb-4">Requests Over Time</h2>
          {chartData.length === 0 ? (
            <p className="text-gray-400 text-center py-12">No data for this time range</p>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                <XAxis
                  dataKey="time"
                  type="number"
                  scale="time"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={(v: number) => formatTickByRange(v, range)}
                  tick={{ fontSize: 12, fill: tickColor }}
                />
                <YAxis allowDecimals={false} tick={{ fill: tickColor }} />
                <Tooltip
                  labelFormatter={(v: number) => new Date(v).toLocaleString()}
                  contentStyle={theme === 'dark' ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6' } : undefined}
                />
                <Legend />
                <Area connectNulls={false} type="monotone" dataKey="request_count" name="Requests" stroke="#6366f1" fill="#6366f1" fillOpacity={0.1} />
                <Area connectNulls={false} type="monotone" dataKey="success_count" name="Successes" stroke="#22c55e" fill="#22c55e" fillOpacity={0.1} />
                <Area connectNulls={false} type="monotone" dataKey="failure_count" name="Failures" stroke="#ef4444" fill="#ef4444" fillOpacity={0.1} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </Card>

        {/* Bytes Over Time */}
        <Card className="p-6 min-w-0">
          <h2 className="text-xl font-semibold mb-4">Bytes Over Time</h2>
          {chartData.length === 0 ? (
            <p className="text-gray-400 text-center py-12">No data for this time range</p>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                <XAxis
                  dataKey="time"
                  type="number"
                  scale="time"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={(v: number) => formatTickByRange(v, range)}
                  tick={{ fontSize: 12, fill: tickColor }}
                />
                <YAxis tickFormatter={(v: number) => formatBytes(v)} tick={{ fill: tickColor }} />
                <Tooltip
                  labelFormatter={(v: number) => new Date(v).toLocaleString()}
                  formatter={(value: number) => formatBytes(value)}
                  contentStyle={theme === 'dark' ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6' } : undefined}
                />
                <Legend />
                <Area connectNulls={false} type="monotone" dataKey="bytes_sent" name="Bytes Sent" stroke="#f97316" fill="#f97316" fillOpacity={0.1} />
                <Area connectNulls={false} type="monotone" dataKey="bytes_received" name="Bytes Received" stroke="#14b8a6" fill="#14b8a6" fillOpacity={0.1} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </Card>
      </div>
    </div>
  )
}

function StatCard({
  title,
  value,
  color = 'text-gray-900 dark:text-gray-100',
  icon,
}: {
  title: string
  value: string
  color?: string
  icon?: React.ReactNode
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center gap-1 mb-1">
        {icon}
        <p className="text-gray-500 dark:text-gray-400 text-sm">{title}</p>
      </div>
      <p className={`text-2xl font-bold ${color}`}>{value}</p>
    </Card>
  )
}
