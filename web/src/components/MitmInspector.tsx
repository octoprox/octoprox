// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef, Row } from '@tanstack/react-table'
import { ChevronDown, ChevronRight, Info, Trash2, Search } from 'lucide-react'
import {
  fetchMitmRequests,
  clearMitmRequests,
  MitmRequestRecord,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { DataTable } from './DataTable'
import { formatBytes } from '../utils/format'
import { Badge, Button } from './ui'

function StatusCodeBadge({ code }: { code: number }) {
  let color: 'green' | 'blue' | 'yellow' | 'red' | 'gray' = 'gray'
  if (code >= 200 && code < 300) color = 'green'
  else if (code >= 300 && code < 400) color = 'blue'
  else if (code >= 400 && code < 500) color = 'yellow'
  else if (code >= 500) color = 'red'
  return <Badge color={color}>{code}</Badge>
}

function MethodBadge({ method }: { method: string }) {
  const colorMap: Record<string, 'blue' | 'green' | 'yellow' | 'red' | 'purple' | 'orange' | 'gray'> = {
    GET: 'blue',
    POST: 'green',
    PUT: 'yellow',
    PATCH: 'orange',
    DELETE: 'red',
    HEAD: 'purple',
    OPTIONS: 'gray',
  }
  return <Badge color={colorMap[method] || 'gray'}>{method}</Badge>
}

function HeadersTable({ headers }: { headers: [string, string][] }) {
  if (headers.length === 0) {
    return <span className="text-xs text-gray-400 italic">No headers</span>
  }
  return (
    <div className="bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700 overflow-auto h-48">
      <table className="w-full text-xs font-mono">
        <tbody>
          {headers.map(([key, value], i) => (
            <tr key={`${key}-${i}`} className="border-b border-gray-100 dark:border-gray-700 last:border-0">
              <td className="px-2 py-1 font-semibold text-gray-600 dark:text-gray-400 whitespace-nowrap align-top">{key}</td>
              <td className="px-2 py-1 text-gray-800 dark:text-gray-200 break-all">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TlsInfoTable({ record }: { record: MitmRequestRecord }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700 overflow-auto h-48">
      <table className="w-full text-xs font-mono">
        <tbody>
          <tr className="border-b border-gray-100 dark:border-gray-700">
            <td className="px-2 py-1 font-semibold text-gray-600 dark:text-gray-400 whitespace-nowrap align-top">Version</td>
            <td className="px-2 py-1 text-gray-800 dark:text-gray-200">{record.tls_version}</td>
          </tr>
          <tr className="border-b border-gray-100 dark:border-gray-700">
            <td className="px-2 py-1 font-semibold text-gray-600 dark:text-gray-400 whitespace-nowrap align-top">Cipher</td>
            <td className="px-2 py-1 text-gray-800 dark:text-gray-200">{record.tls_cipher}</td>
          </tr>
          <tr className="border-b border-gray-100 dark:border-gray-700">
            <td className="px-2 py-1 font-semibold text-gray-600 dark:text-gray-400 whitespace-nowrap align-top">Key Bits</td>
            <td className="px-2 py-1 text-gray-800 dark:text-gray-200">{record.tls_key_bits}</td>
          </tr>
          {record.tls_shared_ciphers.length > 0 && (
            <tr>
              <td className="px-2 py-1 font-semibold text-gray-600 dark:text-gray-400 whitespace-nowrap align-top">Shared Ciphers</td>
              <td className="px-2 py-1 text-gray-800 dark:text-gray-200 break-all">{record.tls_shared_ciphers.join(', ')}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

type DetailTab = 'request' | 'forwarded' | 'response' | 'tls'

function ExpandedDetail({ record }: { record: MitmRequestRecord }) {
  const hasTls = !!record.tls_version
  const tabs: { key: DetailTab; label: string; tooltip?: string }[] = [
    { key: 'request', label: 'Request Headers' },
    { key: 'forwarded', label: 'Forwarded to Relay', tooltip: `Headers passed to the ${record.mitm_engine || 'relay'} engine. Impersonation engines add browser headers (UA, Accept-Encoding, Sec-Fetch-*, etc.) automatically.` },
    { key: 'response', label: 'Response Headers' },
    ...(hasTls ? [{ key: 'tls' as DetailTab, label: 'Client TLS' }] : []),
  ]
  const [activeTab, setActiveTab] = useState<DetailTab>('request')

  return (
    <div className="p-4 bg-gray-50 dark:bg-gray-900 space-y-4">
      <div>
        <div className="flex gap-0 border-b border-gray-200 dark:border-gray-700 mb-3">
          {tabs.map(({ key, label, tooltip }) => (
            <button
              key={key}
              onClick={() => setActiveTab(key)}
              className={`px-3 py-1.5 text-xs font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5 ${
                activeTab === key
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
              }`}
            >
              {label}
              {tooltip && (
                <span className="relative group">
                  <Info className="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 cursor-help" />
                  <span className="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 w-56 px-2.5 py-1.5 rounded bg-gray-900 dark:bg-gray-100 text-[10px] leading-tight text-gray-100 dark:text-gray-900 opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 pointer-events-none">
                    {tooltip}
                  </span>
                </span>
              )}
            </button>
          ))}
        </div>
        {activeTab === 'request' && <HeadersTable headers={record.request_headers} />}
        {activeTab === 'forwarded' && <HeadersTable headers={record.upstream_headers} />}
        {activeTab === 'response' && <HeadersTable headers={record.response_headers} />}
        {activeTab === 'tls' && hasTls && <TlsInfoTable record={record} />}
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Full URL: <span className="font-mono text-gray-700 dark:text-gray-300">{record.url}</span></span>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Mode: <span className="text-gray-700 dark:text-gray-300">{record.mitm_mode}</span></span>
        <span>Engine: <span className="text-gray-700 dark:text-gray-300">{record.mitm_engine || 'n/a'}</span></span>
        <span>Browser: <span className="text-gray-700 dark:text-gray-300">{record.mitm_browser || 'n/a'}</span></span>
        <span>Proxy: <span className="font-mono text-gray-700 dark:text-gray-300">{record.proxy_url}</span></span>
      </div>
    </div>
  )
}

export default function MitmInspector() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()

  const { data, isLoading } = useQuery({
    queryKey: ['mitm-requests', selectedProjectId],
    queryFn: () => fetchMitmRequests(selectedProjectId!, 200),
    enabled: !!selectedProjectId,
    refetchInterval: 5000,
  })

  const clearMutation = useMutation({
    mutationFn: () => clearMitmRequests(selectedProjectId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mitm-requests', selectedProjectId] })
    },
  })

  const columns: ColumnDef<MitmRequestRecord, unknown>[] = useMemo(() => [
    {
      id: 'expand',
      size: 36,
      enableSorting: false,
      cell: ({ row }: { row: Row<MitmRequestRecord> }) => (
        <button
          onClick={row.getToggleExpandedHandler()}
          className="p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
        >
          {row.getIsExpanded()
            ? <ChevronDown className="w-4 h-4" />
            : <ChevronRight className="w-4 h-4" />}
        </button>
      ),
    },
    {
      accessorKey: 'timestamp',
      header: 'Time',
      size: 90,
      cell: ({ getValue }) => {
        const ts = getValue<string>()
        return (
          <span className="font-mono text-xs text-gray-500 dark:text-gray-400">
            {new Date(ts + 'Z').toLocaleTimeString()}
          </span>
        )
      },
    },
    {
      accessorKey: 'method',
      header: 'Method',
      size: 80,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <MethodBadge method={getValue<string>()} />,
    },
    {
      accessorKey: 'status_code',
      header: 'Status',
      size: 80,
      filterFn: 'equalsString',
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <StatusCodeBadge code={getValue<number>()} />,
    },
    {
      accessorKey: 'target_host',
      header: 'Host',
      size: 180,
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => (
        <span className="font-mono text-xs truncate block" title={getValue<string>()}>
          {getValue<string>()}
        </span>
      ),
    },
    {
      id: 'path',
      header: 'Path',
      accessorFn: (row) => {
        try {
          return new URL(row.url).pathname + new URL(row.url).search
        } catch {
          return row.url
        }
      },
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => (
        <span className="font-mono text-xs truncate block" title={getValue<string>()}>
          {getValue<string>()}
        </span>
      ),
    },
    {
      accessorKey: 'latency_ms',
      header: 'Latency',
      size: 80,
      cell: ({ getValue }) => (
        <span className="text-xs">{Math.round(getValue<number>())}ms</span>
      ),
    },
    {
      id: 'size',
      header: 'Size',
      size: 100,
      enableSorting: false,
      cell: ({ row }) => (
        <div className="text-xs leading-tight whitespace-nowrap">
          <div>
            <span className="text-gray-400">Req </span>
            <span className="text-orange-600 dark:text-orange-400">{formatBytes(row.original.request_body_size)}</span>
          </div>
          <div>
            <span className="text-gray-400">Res </span>
            <span className="text-teal-600 dark:text-teal-400">{formatBytes(row.original.response_body_size)}</span>
          </div>
        </div>
      ),
    },
    {
      accessorKey: 'response_content_type',
      header: 'Content-Type',
      size: 140,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => {
        const ct = getValue<string>()
        const short = ct.split(';')[0]
        return (
          <span className="text-xs truncate block" title={ct}>
            {short || '-'}
          </span>
        )
      },
    },
  ], [])

  const records = data?.records ?? []

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">MITM Inspector</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Inspect intercepted HTTPS requests and responses in real time
          </p>
        </div>
        {records.length > 0 && (
          <Button
            variant="ghost"
            onClick={() => clearMutation.mutate()}
            disabled={clearMutation.isPending}
            className="text-red-600 hover:text-red-700 hover:bg-red-50 dark:text-red-400 dark:hover:text-red-300 dark:hover:bg-red-900/20"
          >
            <Trash2 className="w-4 h-4 mr-1.5" />
            Clear
          </Button>
        )}
      </div>

      {isLoading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">Loading...</div>
      ) : records.length === 0 ? (
        <div className="text-center py-16">
          <Search className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-1">No intercepted requests</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Requests will appear here when MITM interception is enabled and traffic flows through the proxy.
          </p>
        </div>
      ) : (
        <DataTable
          columns={columns}
          data={records}
          defaultPageSize={50}
          enableColumnFilters
          getRowId={(row) => row.id}
          renderExpandedRow={(row) => <ExpandedDetail record={row} />}
          emptyMessage="No requests match the current filters."
        />
      )}
    </div>
  )
}
