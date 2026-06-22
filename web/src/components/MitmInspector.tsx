// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef, Row } from '@tanstack/react-table'
import { ChevronDown, ChevronRight, Info, Trash2, Search, Copy, Check } from 'lucide-react'
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
    <div className="bg-surface rounded border border-line overflow-auto h-48">
      <table className="w-full text-xs font-mono">
        <tbody>
          {headers.map(([key, value], i) => (
            <tr key={`${key}-${i}`} className="border-b border-line last:border-0">
              <td className="px-2 py-1 font-semibold text-fg-muted whitespace-nowrap align-top">{key}</td>
              <td className="px-2 py-1 text-fg break-all">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      }}
      className="ml-1.5 p-0.5 text-gray-400 hover:text-fg-muted inline-flex"
      title="Copy to clipboard"
    >
      {copied ? <Check className="w-3 h-3 text-green-500" /> : <Copy className="w-3 h-3" />}
    </button>
  )
}

function TlsRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-2 py-1 font-semibold text-fg-muted whitespace-nowrap align-top w-40">{label}</td>
      <td className="px-2 py-1 text-fg">{children}</td>
    </tr>
  )
}

function TlsSectionHeader({ title }: { title: string }) {
  return (
    <tr className="border-b border-line">
      <td colSpan={2} className="px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-fg-muted bg-surface-raised/60">{title}</td>
    </tr>
  )
}

function IdNameList({ items, maxH = 'max-h-40' }: { items: { id: string | number; name: string }[]; maxH?: string }) {
  if (items.length === 0) return <span className="text-gray-400 italic">None</span>
  return (
    <div className={`overflow-auto ${maxH}`}>
      {items.map((item, i) => (
        <div key={i} className="flex gap-2 leading-relaxed">
          <span className="text-gray-400 shrink-0">{typeof item.id === 'number' ? `0x${item.id.toString(16).padStart(4, '0').toUpperCase()}` : item.id}</span>
          <span className="break-all">{item.name}</span>
        </div>
      ))}
    </div>
  )
}

function TlsInfoTable({ record }: { record: MitmRequestRecord }) {
  const [ja3Expanded, setJa3Expanded] = useState(false)
  const ch = record.tls_client_hello

  return (
    <div className="bg-surface rounded border border-line overflow-auto">
      <table className="w-full text-xs font-mono">
        <tbody>
          <TlsSectionHeader title="Negotiated" />
          <TlsRow label="Version">{record.tls_version}</TlsRow>
          <TlsRow label="Cipher">{record.tls_cipher}</TlsRow>
          <TlsRow label="Key Bits">{record.tls_key_bits}</TlsRow>

          {ch && (
            <>
              <TlsSectionHeader title="Fingerprints" />
              <TlsRow label="JA3">
                <span className="break-all">{ch.ja3}</span>
                <CopyButton text={ch.ja3} />
              </TlsRow>
              <TlsRow label="JA4">
                <span className="break-all">{ch.ja4}</span>
                <CopyButton text={ch.ja4} />
              </TlsRow>
              <TlsRow label="JA4_r">
                <span className="break-all">{ch.ja4_r}</span>
                <CopyButton text={ch.ja4_r} />
              </TlsRow>
              <TlsRow label="JA3 Full">
                {ja3Expanded ? (
                  <div>
                    <span className="break-all">{ch.ja3_full}</span>
                    <CopyButton text={ch.ja3_full} />
                    <button onClick={() => setJa3Expanded(false)} className="ml-2 text-blue-500 hover:text-blue-600 text-[10px]">collapse</button>
                  </div>
                ) : (
                  <div>
                    <span className="text-gray-400 truncate max-w-xs inline-block align-bottom">{ch.ja3_full.slice(0, 60)}...</span>
                    <button onClick={() => setJa3Expanded(true)} className="ml-1 text-blue-500 hover:text-blue-600 text-[10px]">expand</button>
                  </div>
                )}
              </TlsRow>

              <TlsSectionHeader title="Client Hello" />
              {ch.sni && <TlsRow label="SNI">{ch.sni}</TlsRow>}
              {ch.alpn.length > 0 && <TlsRow label="ALPN">{ch.alpn.join(', ')}</TlsRow>}
              {ch.supported_versions.length > 0 && <TlsRow label="Supported Versions">{ch.supported_versions.join(', ')}</TlsRow>}
              <TlsRow label={`Cipher Suites (${ch.cipher_suites.length})`}>
                <IdNameList items={ch.cipher_suites} maxH="max-h-48" />
              </TlsRow>
              <TlsRow label={`Extensions (${ch.extensions.length})`}>
                <IdNameList items={ch.extensions} />
              </TlsRow>
              {ch.supported_groups.length > 0 && (
                <TlsRow label={`Elliptic Curves (${ch.supported_groups.length})`}>
                  <IdNameList items={ch.supported_groups} />
                </TlsRow>
              )}
              {ch.signature_algorithms.length > 0 && (
                <TlsRow label={`Signature Algs (${ch.signature_algorithms.length})`}>
                  <IdNameList items={ch.signature_algorithms} />
                </TlsRow>
              )}
              {ch.ec_point_formats.length > 0 && (
                <TlsRow label="EC Point Formats">
                  {ch.ec_point_formats.map(f => f === 0 ? 'uncompressed' : `${f}`).join(', ')}
                </TlsRow>
              )}
              {ch.record_layer_version && (
                <TlsRow label="Record Layer Version">{ch.record_layer_version}</TlsRow>
              )}
              {ch.session_id_length > 0 && (
                <TlsRow label="Session ID Length">{ch.session_id_length} bytes</TlsRow>
              )}
              {ch.compression_methods?.length > 0 && (
                <TlsRow label="Compression Methods">
                  <IdNameList items={ch.compression_methods} />
                </TlsRow>
              )}
              {ch.key_share_groups?.length > 0 && (
                <TlsRow label={`Key Share (${ch.key_share_groups.length})`}>
                  <div className="overflow-auto max-h-40">
                    {ch.key_share_groups.map((ks, i) => (
                      <div key={i} className="leading-relaxed">
                        {ks.group} <span className="text-gray-400">({ks.key_length} bytes)</span>
                      </div>
                    ))}
                  </div>
                </TlsRow>
              )}
              {ch.compress_certificate?.length > 0 && (
                <TlsRow label="Compress Certificate">
                  <IdNameList items={ch.compress_certificate} />
                </TlsRow>
              )}
              {ch.alps_protocols?.length > 0 && (
                <TlsRow label="ALPS">{ch.alps_protocols.join(', ')}</TlsRow>
              )}
              {ch.psk_key_exchange_modes?.length > 0 && (
                <TlsRow label="PSK KE Modes">
                  <IdNameList items={ch.psk_key_exchange_modes} />
                </TlsRow>
              )}
            </>
          )}

          {!ch && record.tls_shared_ciphers.length > 0 && (
            <TlsRow label="Shared Ciphers">
              <span className="break-all">{record.tls_shared_ciphers.join(', ')}</span>
            </TlsRow>
          )}
        </tbody>
      </table>
    </div>
  )
}

function H2FingerprintTable({ record }: { record: MitmRequestRecord }) {
  if (!record.h2_fingerprint) return null

  const frames = record.h2_frames || []

  return (
    <div className="bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700 overflow-auto">
      <table className="w-full text-xs font-mono">
        <tbody>
          <TlsSectionHeader title="HTTP/2 Fingerprint" />
          <TlsRow label="Akamai Hash">
            <span className="break-all">{record.h2_fingerprint_hash}</span>
            <CopyButton text={record.h2_fingerprint_hash!} />
          </TlsRow>
          <TlsRow label="Akamai Full">
            <span className="break-all">{record.h2_fingerprint}</span>
            <CopyButton text={record.h2_fingerprint!} />
          </TlsRow>

          <TlsSectionHeader title="HTTP/2 Frames" />
          {frames.map((frame, i) => {
            if (frame.type === 'SETTINGS' && frame.settings) {
              return (
                <TlsRow key={i} label="SETTINGS">
                  <div>
                    {Object.entries(frame.settings).map(([name, value]) => (
                      <div key={name} className="leading-relaxed">
                        <span className="text-gray-400">{name}</span> = {value}
                      </div>
                    ))}
                  </div>
                </TlsRow>
              )
            }
            if (frame.type === 'WINDOW_UPDATE') {
              return (
                <TlsRow key={i} label="WINDOW_UPDATE">
                  increment = {frame.delta}
                </TlsRow>
              )
            }
            if (frame.type === 'PRIORITY') {
              return (
                <TlsRow key={i} label="PRIORITY">
                  weight={frame.weight}, depends_on={frame.depends_on}, exclusive={frame.exclusive ? 'yes' : 'no'}
                </TlsRow>
              )
            }
            if (frame.type === 'HEADERS' && frame.pseudo_header_order) {
              return (
                <TlsRow key={i} label="Pseudo-Header Order">
                  {frame.pseudo_header_order.join(', ')}
                </TlsRow>
              )
            }
            return null
          })}
        </tbody>
      </table>
    </div>
  )
}

type DetailTab = 'request' | 'forwarded' | 'response' | 'tls' | 'h2'

function ExpandedDetail({ record }: { record: MitmRequestRecord }) {
  const hasTls = !!record.tls_version
  const hasH2 = !!record.h2_fingerprint
  const tabs: { key: DetailTab; label: string; tooltip?: string }[] = [
    { key: 'request', label: 'Request Headers' },
    { key: 'forwarded', label: 'Forwarded to Relay', tooltip: `Headers passed to the ${record.mitm_engine || 'relay'} engine. Impersonation engines add browser headers (UA, Accept-Encoding, Sec-Fetch-*, etc.) automatically.` },
    { key: 'response', label: 'Response Headers' },
    ...(hasTls ? [{ key: 'tls' as DetailTab, label: 'Client TLS' }] : []),
    ...(hasH2 ? [{ key: 'h2' as DetailTab, label: 'HTTP/2' }] : []),
  ]
  const [activeTab, setActiveTab] = useState<DetailTab>('request')

  return (
    <div className="p-4 bg-bg space-y-4">
      <div>
        <div className="flex gap-0 border-b border-line mb-3">
          {tabs.map(({ key, label, tooltip }) => (
            <button
              key={key}
              onClick={() => setActiveTab(key)}
              className={`px-3 py-1.5 text-xs font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5 ${
                activeTab === key
                  ? 'border-blue-500 text-primary'
                  : 'border-transparent text-fg-muted hover:text-fg'
              }`}
            >
              {label}
              {tooltip && (
                <span className="relative group">
                  <Info className="w-3.5 h-3.5 text-fg-subtle cursor-help" />
                  <span className="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 w-56 px-2.5 py-1.5 rounded bg-fg text-[10px] leading-tight text-bg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 pointer-events-none">
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
        {activeTab === 'h2' && hasH2 && <H2FingerprintTable record={record} />}
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-fg-muted">
        <span>Full URL: <span className="font-mono text-fg">{record.url}</span></span>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-fg-muted">
        <span>Mode: <span className="text-fg">{record.mitm_mode}</span></span>
        <span>Engine: <span className="text-fg">{record.mitm_engine || 'n/a'}</span></span>
        <span>Browser: <span className="text-fg">{record.mitm_browser || 'n/a'}</span></span>
        <span>Proxy: <span className="font-mono text-fg">{record.proxy_url}</span></span>
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
          className="p-0.5 text-gray-400 hover:text-fg-muted"
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
          <span className="font-mono text-xs text-fg-muted">
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
            <span className="text-warning">{formatBytes(row.original.request_body_size)}</span>
          </div>
          <div>
            <span className="text-gray-400">Res </span>
            <span className="text-success">{formatBytes(row.original.response_body_size)}</span>
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
          <h1 className="text-2xl font-bold text-fg">MITM Inspector</h1>
          <p className="text-sm text-fg-muted mt-1">
            Inspect intercepted HTTPS requests and responses in real time
          </p>
        </div>
        {records.length > 0 && (
          <Button
            variant="ghost"
            onClick={() => clearMutation.mutate()}
            disabled={clearMutation.isPending}
            className="text-danger hover:brightness-110 hover:bg-danger-soft"
          >
            <Trash2 className="w-4 h-4 mr-1.5" />
            Clear
          </Button>
        )}
      </div>

      {isLoading ? (
        <div className="text-center py-12 text-fg-muted">Loading...</div>
      ) : records.length === 0 ? (
        <div className="text-center py-16">
          <Search className="w-12 h-12 text-fg-subtle mx-auto mb-4" />
          <h3 className="text-lg font-medium text-fg mb-1">No intercepted requests</h3>
          <p className="text-sm text-fg-muted">
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
