// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link2, Trash2, Pencil, X, Plus, ArrowLeft, AlertTriangle } from 'lucide-react'
import {
  fetchProjectConnectors,
  fetchProjectCredentials,
  fetchProjectCredential,
  fetchConnectorOptions,
  createProjectConnector,
  updateProjectConnector,
  deleteProjectConnector,
  fetchBrightDataZones,
  Connector,
  CredentialType,
  ConnectorCreate,
  ConnectorOptions,
  OxylabsProxyType,
  BrightDataProxyType,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import AddCredentialModal from './AddCredentialModal'
import { CREDENTIAL_TYPES } from '../utils/credentials'
import { RichSelect, RichSelectOption } from './RichSelect'
import { Button, Input, Label, Card, Badge, Alert, ModalFooter } from './ui'

interface ConnectorFormData {
  name: string
  credential_id: string
  config: Record<string, string>
  enabled: boolean
}

// Tab type for config sections
type ConfigTab = 'infrastructure' | 'scaling' | 'advanced'

// Component for editing key-value tags (AWS, Azure)
function KeyValueTagsEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const parseTags = (val: string): Array<{ key: string; value: string }> => {
    try {
      const parsed = JSON.parse(val || '{}')
      return Object.entries(parsed).map(([k, v]) => ({ key: k, value: String(v) }))
    } catch {
      return []
    }
  }

  const [tags, setTags] = useState<Array<{ key: string; value: string }>>(parseTags(value))

  const updateTags = (newTags: Array<{ key: string; value: string }>) => {
    setTags(newTags)
    const obj: Record<string, string> = {}
    newTags.forEach(t => { if (t.key.trim()) obj[t.key.trim()] = t.value })
    onChange(JSON.stringify(obj))
  }

  const addTag = () => updateTags([...tags, { key: '', value: '' }])
  const removeTag = (index: number) => updateTags(tags.filter((_, i) => i !== index))
  const updateTag = (index: number, field: 'key' | 'value', val: string) => {
    const newTags = [...tags]
    newTags[index] = { ...newTags[index], [field]: val }
    updateTags(newTags)
  }

  return (
    <div className="space-y-2">
      {tags.map((tag, index) => (
        <div key={index} className="flex gap-2 items-center">
          <Input type="text" value={tag.key} onChange={(e) => updateTag(index, 'key', e.target.value)} placeholder="Key" className="flex-1 px-3 py-1.5 text-sm" />
          <Input type="text" value={tag.value} onChange={(e) => updateTag(index, 'value', e.target.value)} placeholder="Value" className="flex-1 px-3 py-1.5 text-sm" />
          <button type="button" onClick={() => removeTag(index)} className="p-1.5 text-gray-400 hover:text-red-500"><Trash2 className="w-4 h-4" /></button>
        </div>
      ))}
      <button type="button" onClick={addTag} className="flex items-center gap-1 text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300">
        <Plus className="w-4 h-4" /> Add Tag
      </button>
    </div>
  )
}

// Helper to get the configured number of proxies from connector config
const getConfiguredProxies = (connector: Connector): number | null => {
  const config = connector.config
  const credType = connector.credential_type

  if (!credType || credType === 'static_proxy_provider') {
    return null // Static proxy provider has no configured limit
  }

  if (credType === 'oxylabs' || credType === 'brightdata') {
    return typeof config.num_proxies === 'number' ? config.num_proxies : null
  }

  // Cloud connectors (aws, gcp, azure) use max_proxies
  if (credType === 'aws' || credType === 'gcp' || credType === 'azure') {
    return typeof config.max_proxies === 'number' ? config.max_proxies : null
  }

  return null
}

// Format proxy count display as "X/Y" or just "X" if no configured limit
const formatProxyCount = (connector: Connector): string => {
  const configured = getConfiguredProxies(connector)
  if (configured !== null) {
    return `${connector.proxy_count}/${configured}`
  }
  return `${connector.proxy_count}`
}

const getDefaultConfig = (type: CredentialType | null, options?: ConnectorOptions): Record<string, string> => {
  switch (type) {
    case 'static_proxy_provider':
      return {}
    case 'aws':
      return {
        instance_name: '',
        region: options?.aws_regions[0]?.code || 'us-east-1',
        instance_type: options?.aws_instance_types[0]?.code || 't3.micro',
        key_pair_name: '',
        security_group: '',
        tags: '{}',
        min_proxies: '1',
        max_proxies: '10',
        min_rotation_period_minutes: '60',
        max_rotation_period_minutes: '1440'
      }
    case 'gcp':
      return {
        project_id: '',
        instance_name: '',
        zone: options?.gcp_zones[0]?.code || 'us-central1-a',
        machine_type: options?.gcp_machine_types[0]?.code || 'e2-micro',
        network: 'default',
        tags: '{}',
        min_proxies: '1',
        max_proxies: '10',
        min_rotation_period_minutes: '60',
        max_rotation_period_minutes: '1440'
      }
    case 'azure':
      return {
        subscription_id: '',
        resource_group: '',
        instance_name: '',
        location: options?.azure_locations[0]?.code || 'eastus',
        vm_size: options?.azure_vm_sizes[0]?.code || 'Standard_B1s',
        vnet_name: '',
        subnet_name: '',
        ssh_public_key: '',
        tags: '{}',
        min_proxies: '1',
        max_proxies: '10',
        min_rotation_period_minutes: '60',
        max_rotation_period_minutes: '1440'
      }
    case 'oxylabs':
      return {
        num_proxies: '1',
        country_code: '',
        session_duration_minutes: '10'
      }
    default:
      return {}
  }
}

type WizardStep = 'select-type' | 'select-credential' | 'configure'

export default function ConnectorConfig() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { theme } = useTheme()

  const getlogo = (ct: typeof CREDENTIAL_TYPES[number]) => theme === 'dark' ? ct.logoDark : ct.logo

  // Wizard state
  const [showWizard, setShowWizard] = useState(false)
  const [wizardStep, setWizardStep] = useState<WizardStep>('select-type')
  const [selectedType, setSelectedType] = useState<CredentialType | null>(null)

  // Form state
  const [formData, setFormData] = useState<ConnectorFormData>({ name: '', credential_id: '', config: {}, enabled: true })
  const [editingConnector, setEditingConnector] = useState<Connector | null>(null)

  // Credential creation modal state
  const [showCredentialModal, setShowCredentialModal] = useState(false)

  // Error state for connector forms
  const [createError, setCreateError] = useState<string | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

  // Config tab state
  const [activeConfigTab, setActiveConfigTab] = useState<ConfigTab>('infrastructure')

  const { data: connectorsData, isLoading: connectorsLoading } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const { data: credentialsData } = useQuery({
    queryKey: ['credentials', selectedProjectId],
    queryFn: () => fetchProjectCredentials(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const { data: optionsData } = useQuery({
    queryKey: ['connector-options'],
    queryFn: fetchConnectorOptions,
  })

  // Fetch selected credential details (for Oxylabs proxy type)
  const { data: selectedCredentialData } = useQuery({
    queryKey: ['credential', selectedProjectId, formData.credential_id],
    queryFn: () => fetchProjectCredential(selectedProjectId!, formData.credential_id),
    enabled: !!selectedProjectId && !!formData.credential_id && selectedType === 'oxylabs',
  })

  // Fetch BrightData zones when credential is selected
  const { data: brightDataZones } = useQuery({
    queryKey: ['brightdata-zones', formData.credential_id],
    queryFn: () => fetchBrightDataZones(formData.credential_id),
    enabled: !!formData.credential_id && selectedType === 'brightdata',
  })

  // Helper to get Oxylabs proxy type from selected credential
  const getOxylabsProxyType = (): OxylabsProxyType | null => {
    if (selectedType !== 'oxylabs' || !selectedCredentialData) return null
    return (selectedCredentialData.config as { proxy_type?: OxylabsProxyType })?.proxy_type || null
  }

  // Check if the Oxylabs proxy type is session-based (residential or mobile)
  const isSessionBasedOxylabs = (): boolean => {
    const proxyType = getOxylabsProxyType()
    return proxyType === 'residential' || proxyType === 'mobile'
  }

  // Helper to get BrightData proxy type from selected zone
  const getBrightDataProxyType = (): BrightDataProxyType | null => {
    if (selectedType !== 'brightdata' || !formData.config.zone_name || !brightDataZones) return null
    const zone = brightDataZones.find(z => z.name === formData.config.zone_name)
    return zone?.proxy_type as BrightDataProxyType || null
  }

  // Check if the BrightData proxy type is session-based (residential or mobile)
  const isSessionBasedBrightData = (): boolean => {
    const proxyType = getBrightDataProxyType()
    return proxyType === 'residential' || proxyType === 'mobile'
  }

  const createMutation = useMutation({
    mutationFn: (data: ConnectorCreate) => createProjectConnector(selectedProjectId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      resetWizard()
    },
    onError: (error: Error) => {
      setCreateError(error.message || 'Failed to create connector')
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: { name?: string; credential_id?: string; config?: Record<string, unknown>; enabled?: boolean } }) =>
      updateProjectConnector(selectedProjectId!, id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      setShowWizard(false)
      setWizardStep('select-type')
      setSelectedType(null)
      setFormData({ name: '', credential_id: '', config: {}, enabled: true })
      setEditingConnector(null)
      setEditError(null)
    },
    onError: (error: Error) => {
      setEditError(error.message || 'Failed to update connector')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProjectConnector(selectedProjectId!, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
    },
  })

  const resetWizard = () => {
    setShowWizard(false)
    setWizardStep('select-type')
    setSelectedType(null)
    setFormData({ name: '', credential_id: '', config: {}, enabled: true })
    setCreateError(null)
    setEditingConnector(null)
    setEditError(null)
    setActiveConfigTab('infrastructure')
  }

  const startEditing = (connector: Connector) => {
    setEditingConnector(connector)
    setSelectedType(connector.credential_type as CredentialType)
    const configForForm: Record<string, string> = {}
    const rawConfig = connector.config || {}
    for (const [key, value] of Object.entries(rawConfig)) {
      if (key === 'tags' && typeof value === 'object') {
        configForForm[key] = JSON.stringify(value)
      } else {
        configForForm[key] = String(value ?? '')
      }
    }
    setFormData({
      name: connector.name,
      credential_id: connector.credential_id,
      config: configForForm,
      enabled: connector.enabled,
    })
    setWizardStep('configure')
    setShowWizard(true)
    setEditError(null)
  }

  const handleTypeSelect = (type: CredentialType) => {
    setSelectedType(type)
    setFormData({ ...formData, config: getDefaultConfig(type, optionsData) })
    setWizardStep('select-credential')
  }

  const handleCredentialSelect = (credentialId: string) => {
    if (credentialId === 'create-new') {
      setShowCredentialModal(true)
    } else {
      setFormData({ ...formData, credential_id: credentialId })
      setWizardStep('configure')
    }
  }

  const handleCredentialCreated = (newCredential: { id: string }) => {
    setFormData({ ...formData, credential_id: newCredential.id })
    setShowCredentialModal(false)
    setWizardStep('configure')
  }

  const handleConfigChange = (key: string, value: string) => {
    setFormData({ ...formData, config: { ...formData.config, [key]: value } })
  }

  const isEditMode = !!editingConnector

  const prepareConfigForSubmit = (config: Record<string, string>): Record<string, unknown> => {
    const result: Record<string, unknown> = { ...config }
    if (config.tags) {
      try {
        result.tags = JSON.parse(config.tags)
      } catch {
        result.tags = {}
      }
    }
    return result
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const preparedConfig = prepareConfigForSubmit(formData.config)
    if (isEditMode) {
      updateMutation.mutate({
        id: editingConnector.id,
        data: { name: formData.name, enabled: formData.enabled, config: preparedConfig }
      })
    } else {
      createMutation.mutate({ name: formData.name, credential_id: formData.credential_id, config: preparedConfig, enabled: formData.enabled })
    }
  }

  const getCredentialTypeLabel = (type: string | null) => {
    if (!type) return 'Unknown'
    return CREDENTIAL_TYPES.find(ct => ct.value === type)?.label || type
  }

  const getCredentialsForType = () => {
    return credentialsData?.credentials.filter(c => c.type === selectedType) || []
  }

  const getFieldsByTab = (type: CredentialType): Record<ConfigTab, string[]> => {
    const scalingFields = ['min_proxies', 'max_proxies', 'min_rotation_period_minutes', 'max_rotation_period_minutes']
    const advancedFields = ['tags']
    const infraFields: Record<string, string[]> = {
      aws: ['instance_name', 'region', 'instance_type', 'key_pair_name', 'security_group'],
      gcp: ['project_id', 'instance_name', 'zone', 'machine_type', 'network'],
      azure: ['subscription_id', 'resource_group', 'instance_name', 'location', 'vm_size', 'vnet_name', 'subnet_name', 'ssh_public_key'],
    }
    return {
      infrastructure: infraFields[type] || [],
      scaling: scalingFields,
      advanced: advancedFields,
    }
  }

  const getFieldLabel = (key: string): string => {
    const labels: Record<string, string> = {
      instance_name: 'Instance Name', region: 'Region', instance_type: 'Instance Type',
      key_pair_name: 'Key Pair', security_group: 'Security Group', project_id: 'Project ID',
      zone: 'Zone', machine_type: 'Machine Type', network: 'Network', subnetwork: 'Subnetwork',
      ssh_key: 'SSH Key', ssh_public_key: 'SSH Public Key', subscription_id: 'Subscription ID',
      num_proxies: 'Number of Proxies', country_code: 'Country', session_duration_minutes: 'Session Duration',
      location: 'Location', vm_size: 'VM Size', resource_group: 'Resource Group',
      vnet_name: 'Virtual Network', subnet_name: 'Subnet', min_proxies: 'Min Proxies',
      max_proxies: 'Max Proxies', min_rotation_period_minutes: 'Min Rotation (min)',
      max_rotation_period_minutes: 'Max Rotation (min)',
    }
    return labels[key] || key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())
  }

  const toInstanceTypeOptions = (opts: { code: string; vcpus: number; memory_gb: number; architecture: string; description: string }[] | undefined): RichSelectOption[] => {
    if (!opts) return []
    return opts.map((opt) => ({ value: opt.code, label: opt.code, description: opt.description, badge: opt.architecture }))
  }

  const toRegionOptions = (opts: { code: string; name: string }[] | undefined): RichSelectOption[] => {
    if (!opts) return []
    return opts.map((opt) => ({ value: opt.code, label: opt.name, description: opt.code }))
  }

  const renderConfigFields = (type: CredentialType | null, config: Record<string, string>, onChange: (key: string, value: string) => void) => {
    if (!type || type === 'static_proxy_provider') return null

    // Special handling for Oxylabs - simpler config without tabs
    if (type === 'oxylabs') {
      const proxyType = getOxylabsProxyType()
      const isSessionBased = isSessionBasedOxylabs()
      const countryOptions: RichSelectOption[] = [
        { value: '', label: 'All Countries', description: 'No geo-targeting' },
        ...(optionsData?.oxylabs_countries || []).map((c) => ({
          value: c.code,
          label: c.name,
          description: c.code,
        })),
      ]

      return (
        <div className="mt-4 space-y-4">
          <div>
            <Label className="text-xs text-gray-600 dark:text-gray-400">
              Number of Proxies
              <span className="text-red-500 ml-1">*</span>
            </Label>
            <Input
              type="number"
              min="1"
              value={config.num_proxies || '1'}
              onChange={(e) => onChange('num_proxies', e.target.value)}
              className="px-3 py-1.5 text-sm"
              placeholder="1"
              required
            />
          </div>

          {isSessionBased && (
            <>
              <div>
                <Label className="text-xs text-gray-600 dark:text-gray-400">
                  Country
                </Label>
                <RichSelect
                  options={countryOptions}
                  value={config.country_code || ''}
                  onChange={(val) => onChange('country_code', val)}
                  placeholder="Select country (optional)"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Leave as "All Countries" for no geo-targeting
                </p>
              </div>

              <div>
                <Label className="text-xs text-gray-600 dark:text-gray-400">
                  Session Duration (minutes)
                  <span className="text-red-500 ml-1">*</span>
                </Label>
                <Input
                  type="number"
                  min="1"
                  max="30"
                  value={config.session_duration_minutes || '10'}
                  onChange={(e) => onChange('session_duration_minutes', e.target.value)}
                  className="px-3 py-1.5 text-sm"
                  placeholder="10"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Session duration: 1-30 minutes (default: 10)
                </p>
              </div>
            </>
          )}

          {proxyType && !isSessionBased && (
            <p className="text-sm text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 p-3 rounded-lg">
              Port-based proxy type ({proxyType}). IPs will be discovered from Oxylabs ports and refreshed every 24 hours.
            </p>
          )}

          {!proxyType && (
            <p className="text-sm text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 p-3 rounded-lg">
              Loading credential configuration...
            </p>
          )}
        </div>
      )
    }

    // Special handling for BrightData - zone selection and config
    if (type === 'brightdata') {
      const proxyType = getBrightDataProxyType()
      const isSessionBased = isSessionBasedBrightData()
      const countryOptions: RichSelectOption[] = [
        { value: '', label: 'All Countries', description: 'No geo-targeting' },
        ...(optionsData?.oxylabs_countries || []).map((c) => ({
          value: c.code,
          label: c.name,
          description: c.code,
        })),
      ]

      const zoneOptions: RichSelectOption[] = (brightDataZones || []).map((zone) => ({
        value: zone.name,
        label: zone.name,
        description: `${zone.proxy_type} (${zone.type})`,
      }))

      return (
        <div className="mt-4 space-y-4">
          <div>
            <Label className="text-xs text-gray-600 dark:text-gray-400">
              Zone
              <span className="text-red-500 ml-1">*</span>
            </Label>
            <RichSelect
              options={zoneOptions}
              value={config.zone_name || ''}
              onChange={(val) => {
                // When zone changes, auto-populate zone_password and proxy_type
                const zone = brightDataZones?.find(z => z.name === val)
                if (zone) {
                  // Update all fields at once to avoid stale state issues
                  setFormData(prev => ({
                    ...prev,
                    config: {
                      ...prev.config,
                      zone_name: val,
                      zone_password: zone.password,
                      proxy_type: zone.proxy_type,
                    }
                  }))
                }
              }}
              placeholder="Select zone"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Select a BrightData zone. Proxy type is determined by the zone.
            </p>
          </div>

          <div>
            <Label className="text-xs text-gray-600 dark:text-gray-400">
              Zone Password
              <span className="text-red-500 ml-1">*</span>
            </Label>
            <Input
              type="text"
              value={config.zone_password || ''}
              onChange={(e) => onChange('zone_password', e.target.value)}
              className="px-3 py-1.5 text-sm"
              placeholder="Auto-populated from zone"
              required
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Auto-populated when selecting a zone. Can be edited if needed.
            </p>
          </div>

          <div>
            <Label className="text-xs text-gray-600 dark:text-gray-400">
              Number of Proxies
              <span className="text-red-500 ml-1">*</span>
            </Label>
            <Input
              type="number"
              min="1"
              value={config.num_proxies || '1'}
              onChange={(e) => onChange('num_proxies', e.target.value)}
              className="px-3 py-1.5 text-sm"
              placeholder="1"
              required
            />
          </div>

          <div>
            <Label className="text-xs text-gray-600 dark:text-gray-400">
              Country
            </Label>
            <RichSelect
              options={countryOptions}
              value={config.country_code || ''}
              onChange={(val) => onChange('country_code', val)}
              placeholder="Select country (optional)"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Leave as "All Countries" for no geo-targeting. Supported by all proxy types.
            </p>
          </div>

          <div>
            <Label className="text-xs text-gray-600 dark:text-gray-400">
              Healthcheck URL
            </Label>
            <Input
              type="url"
              value={config.healthcheck_url || ''}
              onChange={(e) => onChange('healthcheck_url', e.target.value)}
              className="px-3 py-1.5 text-sm"
              placeholder="https://httpbin.org/ip (default)"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Custom URL for health checks. Use this if your BrightData zone is restricted to certain URLs.
            </p>
          </div>

          {proxyType && !isSessionBased && (
            <p className="text-sm text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 p-3 rounded-lg">
              Port-based proxy type ({proxyType}). IPs will be discovered and refreshed every 24 hours.
            </p>
          )}

          {proxyType && isSessionBased && (
            <p className="text-sm text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 p-3 rounded-lg">
              Session-based proxy type ({proxyType}). Global session IDs will be used for routing.
            </p>
          )}

          {!config.zone_name && (
            <p className="text-sm text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 p-3 rounded-lg">
              Please select a zone to continue.
            </p>
          )}
        </div>
      )
    }

    const regionDropdownFields: Record<string, RichSelectOption[]> = {
      region: toRegionOptions(optionsData?.aws_regions),
      zone: toRegionOptions(optionsData?.gcp_zones),
      location: toRegionOptions(optionsData?.azure_locations),
    }
    const instanceTypeDropdownFields: Record<string, RichSelectOption[]> = {
      instance_type: toInstanceTypeOptions(optionsData?.aws_instance_types),
      machine_type: toInstanceTypeOptions(optionsData?.gcp_machine_types),
      vm_size: toInstanceTypeOptions(optionsData?.azure_vm_sizes),
    }
    const requiredFields: Record<string, string[]> = {
      aws: ['instance_name', 'region', 'instance_type', 'key_pair_name', 'security_group'],
      gcp: ['project_id', 'instance_name', 'zone', 'machine_type'],
      azure: ['subscription_id', 'resource_group', 'instance_name', 'vm_size', 'ssh_public_key'],
    }
    const isRequired = (key: string) => requiredFields[type]?.includes(key) || false

    const getPlaceholder = (key: string): string => {
      const placeholders: Record<string, string> = {
        security_group: 'sg-xxxxxxxx', key_pair_name: 'my-key-pair', instance_name: 'proxy-instance',
        project_id: 'my-gcp-project', subscription_id: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
        resource_group: 'my-resource-group', vnet_name: 'my-vnet', subnet_name: 'default',
        ssh_public_key: 'ssh-rsa AAAA... user@host',
      }
      return placeholders[key] || getFieldLabel(key)
    }

    const getHelperText = (key: string): string | null => {
      const helpers: Record<string, string> = {}
      return helpers[key] || null
    }

    const fieldsByTab = getFieldsByTab(type)
    const currentFields = fieldsByTab[activeConfigTab]
    const isNumber = (key: string) => ['min_proxies', 'max_proxies', 'min_rotation_period_minutes', 'max_rotation_period_minutes'].includes(key)

    const tabs: { id: ConfigTab; label: string }[] = [
      { id: 'infrastructure', label: 'Infrastructure' },
      { id: 'scaling', label: 'Scaling' },
      { id: 'advanced', label: 'Advanced' },
    ]

    return (
      <div className="mt-4">
        {/* Tab Navigation */}
        <div className="flex border-b border-gray-200 dark:border-gray-600 mb-4">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveConfigTab(tab.id)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeConfigTab === tab.id
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:border-gray-300 dark:hover:border-gray-500'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Tab Content */}
        <div className="min-h-[200px]">
          {activeConfigTab === 'advanced' ? (
            <div>
              <Label>Instance Tags</Label>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">Key-value tags applied to instances</p>
              <KeyValueTagsEditor value={config.tags || '{}'} onChange={(val) => onChange('tags', val)} />
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              {currentFields.map((key) => {
                const regionOptions = regionDropdownFields[key]
                const instanceTypeOptions = instanceTypeDropdownFields[key]
                const helperText = getHelperText(key)
                const required = isRequired(key)
                return (
                  <div key={key}>
                    <Label className="text-xs text-gray-600 dark:text-gray-400">
                      {getFieldLabel(key)}
                      {required && <span className="text-red-500 ml-1">*</span>}
                    </Label>
                    {regionOptions && regionOptions.length > 0 ? (
                      <RichSelect
                        options={regionOptions}
                        value={config[key] || ''}
                        onChange={(val) => onChange(key, val)}
                        placeholder={`Select ${getFieldLabel(key).toLowerCase()}`}
                        required={required}
                      />
                    ) : instanceTypeOptions && instanceTypeOptions.length > 0 ? (
                      <RichSelect
                        options={instanceTypeOptions}
                        value={config[key] || ''}
                        onChange={(val) => onChange(key, val)}
                        placeholder={`Select ${getFieldLabel(key).toLowerCase()}`}
                        required={required}
                      />
                    ) : (
                      <Input
                        type={isNumber(key) ? 'number' : 'text'}
                        value={config[key] || ''}
                        onChange={(e) => onChange(key, e.target.value)}
                        className="px-3 py-1.5 text-sm"
                        placeholder={getPlaceholder(key)}
                        required={required}
                      />
                    )}
                    {helperText && <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{helperText}</p>}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    )
  }

  if (connectorsLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Connectors</h1>
        <Button onClick={() => setShowWizard(true)}>
          <Link2 className="w-5 h-5" /> Add Connector
        </Button>
      </div>

      {/* Wizard Modal - custom layout for multi-step wizard */}
      {showWizard && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-4xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div className="p-6 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
              <div className="flex items-center gap-4">
                {wizardStep !== 'select-type' && !isEditMode && (
                  <Button variant="ghost" size="icon" onClick={() => setWizardStep(wizardStep === 'configure' ? 'select-credential' : 'select-type')}>
                    <ArrowLeft className="w-5 h-5" />
                  </Button>
                )}
                <h2 className="text-xl font-semibold">
                  {isEditMode ? 'Edit Connector' : (
                    <>
                      {wizardStep === 'select-type' && 'Select Connector Type'}
                      {wizardStep === 'select-credential' && `Select ${getCredentialTypeLabel(selectedType)} Credential`}
                      {wizardStep === 'configure' && 'Configure Connector'}
                    </>
                  )}
                </h2>
              </div>
              <Button variant="ghost" size="icon" onClick={resetWizard}><X className="w-5 h-5" /></Button>
            </div>

            <div className="p-6">
              {/* Step 1: Select Type */}
              {wizardStep === 'select-type' && (
                <div className="grid grid-cols-2 gap-4">
                  {CREDENTIAL_TYPES.map((ct) => (
                    <button key={ct.value} onClick={() => handleTypeSelect(ct.value)} className="flex items-center gap-4 p-6 border-2 border-gray-200 dark:border-gray-600 rounded-xl hover:border-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/30 transition-all text-left">
                      <img src={getlogo(ct)} alt={ct.name} className="w-16 h-16 object-contain" />
                      <div>
                        <h3 className="text-lg font-semibold">{ct.name}</h3>
                        <p className="text-gray-500 dark:text-gray-400 text-sm">{ct.description}</p>
                      </div>
                    </button>
                  ))}
                </div>
              )}

              {/* Step 2: Select Credential */}
              {wizardStep === 'select-credential' && (
                <div className="space-y-4">
                  {getCredentialsForType().length === 0 ? (
                    <div className="text-center py-8">
                      <p className="text-gray-500 dark:text-gray-400 mb-4">No {getCredentialTypeLabel(selectedType)} credentials found.</p>
                      <Button onClick={() => handleCredentialSelect('create-new')} className="mx-auto">
                        <Plus className="w-5 h-5" /> Create New Credential
                      </Button>
                    </div>
                  ) : (
                    <>
                      {getCredentialsForType().map((cred) => (
                        <button key={cred.id} onClick={() => handleCredentialSelect(cred.id)} className={`w-full flex items-center justify-between p-4 border-2 rounded-lg hover:border-blue-500 transition-all ${formData.credential_id === cred.id ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30' : 'border-gray-200 dark:border-gray-600'}`}>
                          <span className="font-medium">{cred.name}</span>
                          <span className="text-gray-500 dark:text-gray-400 text-sm">Created {new Date(cred.created_at).toLocaleDateString()}</span>
                        </button>
                      ))}
                      <button onClick={() => handleCredentialSelect('create-new')} className="w-full flex items-center justify-center gap-2 p-4 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg hover:border-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/30 transition-all text-gray-600 dark:text-gray-400">
                        <Plus className="w-5 h-5" /> Create New Credential
                      </button>
                    </>
                  )}
                </div>
              )}

              {/* Step 3: Configure */}
              {wizardStep === 'configure' && (
                <form onSubmit={handleSubmit}>
                  {(isEditMode ? editError : createError) && (
                    <Alert className="mb-4">
                      {isEditMode ? editError : createError}
                    </Alert>
                  )}
                  <div className="grid gap-4">
                    <div>
                      <Label>Connector Name</Label>
                      <Input type="text" value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} placeholder="My Connector" required />
                    </div>
                    <label className="flex items-center gap-2">
                      <input type="checkbox" checked={formData.enabled} onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })} className="w-4 h-4" /> Enabled
                    </label>
                    {renderConfigFields(selectedType, formData.config, handleConfigChange)}
                  </div>
                  <ModalFooter className="gap-4">
                    <Button type="button" variant="outline" onClick={resetWizard}>Cancel</Button>
                    <Button type="submit" variant="success" disabled={createMutation.isPending || updateMutation.isPending}>
                      {isEditMode
                        ? (updateMutation.isPending ? 'Saving...' : 'Save Changes')
                        : (createMutation.isPending ? 'Creating...' : 'Create Connector')}
                    </Button>
                  </ModalFooter>
                </form>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Credential Creation Modal */}
      <AddCredentialModal
        isOpen={showCredentialModal}
        onClose={() => setShowCredentialModal(false)}
        onSuccess={handleCredentialCreated}
        fixedType={selectedType || undefined}
      />

      {/* Connector List */}
      {connectorsData?.connectors.length === 0 ? (
        <Card className="p-8 text-center">
          <Link2 className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100">No connectors configured</h3>
          <p className="text-gray-500 dark:text-gray-400 mt-2">Add a connector to start managing proxies from your credentials.</p>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">Connector</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">Credential</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">Proxies</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">Status</th>
                <th className="px-4 py-2 text-right text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
              {connectorsData?.connectors.map((connector) => (
                <tr key={connector.id} className="hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors">
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-3">
                      <img src={(() => { const ct = CREDENTIAL_TYPES.find(ct => ct.value === connector.credential_type); return ct ? getlogo(ct) : undefined })()} alt="" className="w-6 h-6 object-contain" />
                      <span className="font-medium text-gray-900 dark:text-gray-100">{connector.name}</span>
                    </div>
                  </td>
                  <td className="px-4 py-2 text-gray-500 dark:text-gray-400">
                    {connector.credential_name || 'Unknown'}
                    <span className="text-gray-400 dark:text-gray-500 ml-1">({getCredentialTypeLabel(connector.credential_type)})</span>
                  </td>
                  <td className="px-4 py-2 text-gray-500 dark:text-gray-400">{formatProxyCount(connector)}</td>
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-2">
                      {connector.last_error && (
                        <Badge color="red" className="text-xs flex items-center gap-1" title={connector.last_error}>
                          <AlertTriangle className="w-3 h-3" />
                          Error
                        </Badge>
                      )}
                      <Badge color={connector.enabled ? 'green' : 'gray'} className="text-xs">
                        {connector.enabled ? 'Enabled' : 'Disabled'}
                      </Badge>
                    </div>
                  </td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <Button variant="ghost" size="icon" onClick={() => startEditing(connector)} className="text-gray-500 hover:text-blue-600">
                        <Pencil className="w-4 h-4" />
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => deleteMutation.mutate(connector.id)} className="text-gray-500 hover:text-red-600">
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}
