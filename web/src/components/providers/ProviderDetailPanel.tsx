// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Copy, Download, ExternalLink } from 'lucide-react'
import { exportProviderYaml, fetchProvider, ProviderSummary } from '../../api/client'
import { useProviders } from '../../hooks/useProviders'
import { Badge, Button, Inspector, InspectorSection, KeyValue, QueryContent, Tabs, INSPECTOR_WIDTH_WIDE } from '../ui'
import { ProviderLogo } from '../ProviderLogo'
import { TestPanel } from './TestPanel'
import { EntityHistory } from '../audit/EntityHistory'
import { formatDateTime } from '../../utils/format'

type Tab = 'details' | 'test' | 'history'

/**
 * Read-only view of any provider; built-ins can be exported or duplicated into a
 * custom one. Descriptor providers also get a Test tab, so a shipped vendor can be
 * tried with throwaway credentials before anything is created, and custom ones a
 * History tab, since only they are changed here.
 */
export function ProviderDetailPanel({ provider, onClose, onDuplicate, onEdit }: {
  provider: ProviderSummary
  onClose: () => void
  onDuplicate: (spec: Record<string, any>) => void
  onEdit?: () => void
}) {
  const detailQuery = useQuery({ queryKey: ['provider', provider.id], queryFn: () => fetchProvider(provider.id) })
  const { data: detail } = detailQuery
  const { presets } = useProviders()
  const [tab, setTab] = useState<Tab>('details')
  const testable = provider.kind === 'descriptor'
  const custom = provider.source === 'custom'
  const tabs: { id: Tab; label: string }[] = [
    { id: 'details', label: 'Details' },
    ...(testable ? [{ id: 'test' as const, label: 'Test' }] : []),
    ...(custom ? [{ id: 'history' as const, label: 'History' }] : []),
  ]
  const download = async () => {
    const yaml = await exportProviderYaml(provider.id)
    const blob = new Blob([yaml], { type: 'application/yaml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `${provider.id}.yaml`; a.click()
    URL.revokeObjectURL(url)
  }
  return (
    <Inspector
      title={provider.name}
      subtitle={<span className="font-mono">{provider.id}</span>}
      onClose={onClose}
      width={INSPECTOR_WIDTH_WIDE}
      footer={(
        <>
          {provider.kind === 'descriptor' && <Button type="button" variant="outline" size="sm" onClick={download}><Download className="w-3.5 h-3.5" /> Export YAML</Button>}
          <span className="flex-1" />
          {provider.kind === 'descriptor' && detail?.spec && <Button type="button" variant="outline" size="sm" onClick={() => onDuplicate(detail.spec!)}><Copy className="w-3.5 h-3.5" /> Duplicate as custom</Button>}
          {onEdit && <Button type="button" size="sm" onClick={onEdit}>Edit</Button>}
        </>
      )}
    >
      {tabs.length > 1 && <Tabs<Tab> tabs={tabs} active={tab} onChange={setTab} size="sm" className={custom ? 'pointer-coarse:*:min-h-11' : undefined} />}
      {tab === 'test' && testable ? (
        <QueryContent query={detailQuery} what="the provider">
          {(loaded) => loaded.spec && <TestPanel key={provider.id} spec={loaded.spec} presets={presets} stored={provider} />}
        </QueryContent>
      ) : tab === 'history' && custom ? (
        <EntityHistory entityType="provider" entityId={provider.id} />
      ) : (<>
      <div className="flex items-center gap-3 p-3 border border-line rounded-[10px]">
        <ProviderLogo type={provider.id} name={provider.name} className="w-10 h-10 text-[40px]" />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold flex items-center gap-2">
            {provider.name}
            <Badge color={provider.source === 'custom' ? 'purple' : provider.kind === 'code' ? 'slate' : 'blue'} className="py-0 text-[10px]">{provider.source === 'builtin' && provider.kind === 'code' ? 'built-in code' : provider.source}</Badge>
            {provider.beta && <Badge color="yellow" className="py-0 text-[10px]">beta</Badge>}
          </div>
          <div className="text-xs text-fg-muted">{provider.description}</div>
          {provider.docs_url && <a href={provider.docs_url} target="_blank" rel="noreferrer" className="text-xs text-primary inline-flex items-center gap-1 mt-0.5">Documentation <ExternalLink className="w-3 h-3" /></a>}
        </div>
      </div>
      <InspectorSection title="Usage">
        <KeyValue label="Credentials" value={provider.credential_count} />
        <KeyValue label="Connectors" value={provider.connector_count} />
        {provider.updated_at && <KeyValue label="Updated" value={formatDateTime(provider.updated_at)} />}
        <KeyValue label="Version" value={provider.version} />
      </InspectorSection>
      {provider.kind === 'descriptor' && (
        <InspectorSection title="Proxy types">
          {provider.proxy_types.map((t) => <KeyValue key={t.key} label={t.label} value={<Badge color="blue" className="py-0 text-[10px]">{t.mode}</Badge>} />)}
        </InspectorSection>
      )}
      <InspectorSection title="Fields">
        <KeyValue label="Credential" value={provider.credential_fields.map((f) => f.key).join(', ') || '-'} mono />
        <KeyValue label="Connector" value={provider.connector_fields.map((f) => f.key).join(', ') || '-'} mono />
      </InspectorSection>
      <InspectorSection title="Network">
        <KeyValue label="Credentials sent to" value={provider.egress_hosts.join(', ') || 'no vendor API calls'} mono />
        {provider.gateway_hosts.length > 0 && <KeyValue label="Proxy gateways" value={provider.gateway_hosts.join(', ')} mono />}
        <KeyValue label="Validates credentials" value={provider.has_validation ? 'yes' : 'no'} />
      </InspectorSection>
      </>)}
    </Inspector>
  )
}
