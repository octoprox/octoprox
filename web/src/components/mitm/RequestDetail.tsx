// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Copy, Check, Info } from 'lucide-react'
import { MitmRequestRecord } from '../../api/client'
import { formatBytes, formatTime } from '../../utils/format'
import { Badge, Inspector, KeyValue, StatGrid, Tabs } from '../ui'

export function StatusCodeBadge({ code }: { code: number }) {
  let color: 'green' | 'blue' | 'yellow' | 'red' | 'gray' = 'gray'
  if (code >= 200 && code < 300) color = 'green'
  else if (code >= 300 && code < 400) color = 'blue'
  else if (code >= 400 && code < 500) color = 'yellow'
  else if (code >= 500) color = 'red'
  return <Badge color={color}>{code}</Badge>
}

export function MethodBadge({ method }: { method: string }) {
  const colorMap: Record<string, 'blue' | 'green' | 'yellow' | 'red' | 'purple' | 'orange' | 'gray'> = {
    GET: 'blue', POST: 'green', PUT: 'yellow', PATCH: 'orange', DELETE: 'red', HEAD: 'purple', OPTIONS: 'gray',
  }
  return <Badge color={colorMap[method] || 'gray'}>{method}</Badge>
}

function HeadersTable({ headers }: { headers: [string, string][] }) {
  if (headers.length === 0) return <span className="text-xs text-fg-subtle italic">No headers</span>
  return (
    <div className="rounded-lg border border-line overflow-hidden">
      {headers.map(([key, value], i) => (
        <div key={`${key}-${i}`} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)] gap-x-2.5 px-2.5 py-1.5 border-b border-line last:border-b-0 font-mono text-[11.5px]">
          <span className="font-semibold text-fg-muted truncate" title={key}>{key}</span>
          <span className="text-fg break-all">{value}</span>
        </div>
      ))}
    </div>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
      className="ml-1.5 p-0.5 text-fg-subtle hover:text-fg-muted inline-flex align-middle"
      title="Copy to clipboard"
    >
      {copied ? <Check className="w-3 h-3 text-success" /> : <Copy className="w-3 h-3" />}
    </button>
  )
}

function TlsRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-2.5 px-2.5 py-1.5 border-b border-line last:border-b-0 font-mono text-[11.5px]">
      <span className="font-semibold text-fg-muted">{label}</span>
      <span className="text-fg min-w-0">{children}</span>
    </div>
  )
}

function TlsSectionHeader({ title }: { title: string }) {
  return <div className="px-2.5 py-1.5 text-[10px] font-bold uppercase tracking-wider text-fg-muted bg-surface-raised/60 border-b border-line">{title}</div>
}

function IdNameList({ items, maxH = 'max-h-40' }: { items: { id: string | number; name: string }[]; maxH?: string }) {
  if (items.length === 0) return <span className="text-fg-subtle italic">None</span>
  return (
    <div className={`overflow-auto ${maxH}`}>
      {items.map((item, i) => (
        <div key={i} className="flex gap-2 leading-relaxed">
          <span className="text-fg-subtle shrink-0">{typeof item.id === 'number' ? `0x${item.id.toString(16).padStart(4, '0').toUpperCase()}` : item.id}</span>
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
    <div className="rounded-lg border border-line overflow-hidden">
      <TlsSectionHeader title="Negotiated" />
      <TlsRow label="Version">{record.tls_version}</TlsRow>
      <TlsRow label="Cipher">{record.tls_cipher}</TlsRow>
      <TlsRow label="Key bits">{record.tls_key_bits}</TlsRow>
      {ch && (
        <>
          <TlsSectionHeader title="Fingerprints" />
          <TlsRow label="JA3"><span className="break-all">{ch.ja3}</span><CopyButton text={ch.ja3} /></TlsRow>
          <TlsRow label="JA4"><span className="break-all">{ch.ja4}</span><CopyButton text={ch.ja4} /></TlsRow>
          <TlsRow label="JA4_r"><span className="break-all">{ch.ja4_r}</span><CopyButton text={ch.ja4_r} /></TlsRow>
          <TlsRow label="JA3 full">
            {ja3Expanded ? (
              <div>
                <span className="break-all">{ch.ja3_full}</span>
                <CopyButton text={ch.ja3_full} />
                <button onClick={() => setJa3Expanded(false)} className="ml-2 text-primary text-[10px]">collapse</button>
              </div>
            ) : (
              <div>
                <span className="text-fg-subtle">{ch.ja3_full.slice(0, 48)}…</span>
                <button onClick={() => setJa3Expanded(true)} className="ml-1 text-primary text-[10px]">expand</button>
              </div>
            )}
          </TlsRow>
          <TlsSectionHeader title="Client Hello" />
          {ch.sni && <TlsRow label="SNI">{ch.sni}</TlsRow>}
          {ch.alpn.length > 0 && <TlsRow label="ALPN">{ch.alpn.join(', ')}</TlsRow>}
          {ch.supported_versions.length > 0 && <TlsRow label="Versions">{ch.supported_versions.join(', ')}</TlsRow>}
          <TlsRow label={`Ciphers (${ch.cipher_suites.length})`}><IdNameList items={ch.cipher_suites} maxH="max-h-48" /></TlsRow>
          <TlsRow label={`Extensions (${ch.extensions.length})`}><IdNameList items={ch.extensions} /></TlsRow>
          {ch.supported_groups.length > 0 && <TlsRow label={`Curves (${ch.supported_groups.length})`}><IdNameList items={ch.supported_groups} /></TlsRow>}
          {ch.signature_algorithms.length > 0 && <TlsRow label={`Sig algs (${ch.signature_algorithms.length})`}><IdNameList items={ch.signature_algorithms} /></TlsRow>}
          {ch.ec_point_formats.length > 0 && <TlsRow label="EC point formats">{ch.ec_point_formats.map((f) => (f === 0 ? 'uncompressed' : `${f}`)).join(', ')}</TlsRow>}
          {ch.record_layer_version && <TlsRow label="Record layer">{ch.record_layer_version}</TlsRow>}
          {ch.session_id_length > 0 && <TlsRow label="Session ID">{ch.session_id_length} bytes</TlsRow>}
          {ch.compression_methods?.length > 0 && <TlsRow label="Compression"><IdNameList items={ch.compression_methods} /></TlsRow>}
          {ch.key_share_groups?.length > 0 && (
            <TlsRow label={`Key share (${ch.key_share_groups.length})`}>
              <div className="overflow-auto max-h-40">
                {ch.key_share_groups.map((ks, i) => (
                  <div key={i} className="leading-relaxed">{ks.group} <span className="text-fg-subtle">({ks.key_length} bytes)</span></div>
                ))}
              </div>
            </TlsRow>
          )}
          {ch.compress_certificate?.length > 0 && <TlsRow label="Compress cert"><IdNameList items={ch.compress_certificate} /></TlsRow>}
          {ch.alps_protocols?.length > 0 && <TlsRow label="ALPS">{ch.alps_protocols.join(', ')}</TlsRow>}
          {ch.psk_key_exchange_modes?.length > 0 && <TlsRow label="PSK KE modes"><IdNameList items={ch.psk_key_exchange_modes} /></TlsRow>}
        </>
      )}
      {!ch && record.tls_shared_ciphers.length > 0 && (
        <TlsRow label="Shared ciphers"><span className="break-all">{record.tls_shared_ciphers.join(', ')}</span></TlsRow>
      )}
    </div>
  )
}

type DetailTab = 'request' | 'forwarded' | 'response' | 'tls'

/** Docked panel showing one intercepted request. */
export function RequestPanel({ record, onClose }: { record: MitmRequestRecord; onClose: () => void }) {
  const hasTls = !!record.tls_version
  const tabs: { id: DetailTab; label: string }[] = [
    { id: 'request', label: 'Request' },
    { id: 'forwarded', label: 'Forwarded' },
    { id: 'response', label: 'Response' },
    ...(hasTls ? [{ id: 'tls' as DetailTab, label: 'Client TLS' }] : []),
  ]
  const [tab, setTab] = useState<DetailTab>('request')
  let path = record.url
  try { const u = new URL(record.url); path = u.pathname + u.search } catch { /* keep raw */ }

  return (
    <Inspector
      title="Request"
      subtitle={`captured at ${formatTime(record.timestamp)}`}
      onClose={onClose}
      width={520}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <MethodBadge method={record.method} />
        <StatusCodeBadge code={record.status_code} />
        <span className="text-xs text-fg-muted tabular-nums">{Math.round(record.latency_ms)} ms · {record.mitm_mode}{record.mitm_engine ? ` · ${record.mitm_engine}` : ''}{record.mitm_browser ? ` · ${record.mitm_browser}` : ''}</span>
      </div>
      <div className="font-mono text-xs break-all leading-relaxed">
        <span className="text-fg-muted">{record.target_host}</span>{path}
        <CopyButton text={record.url} />
      </div>
      <StatGrid items={[
        { label: 'Request body', value: formatBytes(record.request_body_size) },
        { label: 'Response body', value: formatBytes(record.response_body_size) },
        { label: 'Content type', value: <span className="text-xs font-medium" title={record.response_content_type}>{record.response_content_type.split(';')[0] || '—'}</span> },
      ]} />
      <KeyValue label="Via proxy" value={record.proxy_url} mono />

      <Tabs<DetailTab> tabs={tabs} active={tab} onChange={setTab} size="sm" />
      {tab === 'forwarded' && (
        <p className="text-xs text-fg-muted flex items-start gap-1.5">
          <Info className="w-3.5 h-3.5 flex-none mt-0.5" />
          Headers passed to the {record.mitm_engine || 'relay'} engine. Impersonation engines add browser headers (UA, Accept-Encoding, Sec-Fetch-*) automatically.
        </p>
      )}
      {tab === 'request' && <HeadersTable headers={record.request_headers} />}
      {tab === 'forwarded' && <HeadersTable headers={record.upstream_headers} />}
      {tab === 'response' && <HeadersTable headers={record.response_headers} />}
      {tab === 'tls' && hasTls && <TlsInfoTable record={record} />}
    </Inspector>
  )
}
