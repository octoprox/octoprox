import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link2, Trash2, Pencil, X, Plus, ArrowLeft } from 'lucide-react'
import {
  fetchProjectConnectors,
  fetchProjectCredentials,
  fetchConnectorOptions,
  createProjectConnector,
  createProjectCredential,
  updateProjectConnector,
  deleteProjectConnector,
  Connector,
  CredentialType,
  ConnectorCreate,
  CredentialCreate,
  ConnectorOptions,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'

// Import logos
import awsLogo from '../assets/logos/aws.svg'
import gcpLogo from '../assets/logos/gcp.svg'
import azureLogo from '../assets/logos/azure.svg'
import staticProxyLogo from '../assets/logos/static-proxy.svg'

interface ConnectorFormData {
  name: string
  credential_id: string
  config: Record<string, string>
  enabled: boolean
}

interface CredentialFormData {
  name: string
  type: CredentialType
  config: Record<string, string>
}

const CONNECTOR_TYPES: { type: CredentialType; name: string; description: string; logo: string }[] = [
  { type: 'static_proxy_provider', name: 'Static Proxy Provider', description: 'Manually managed proxy servers', logo: staticProxyLogo },
  { type: 'aws', name: 'Amazon Web Services', description: 'EC2 instances as proxy servers', logo: awsLogo },
  { type: 'gcp', name: 'Google Cloud Platform', description: 'Compute Engine VMs as proxy servers', logo: gcpLogo },
  { type: 'azure', name: 'Microsoft Azure', description: 'Virtual Machines as proxy servers', logo: azureLogo },
]

const getDefaultConfig = (type: CredentialType | null, options?: ConnectorOptions): Record<string, string> => {
  switch (type) {
    case 'static_proxy_provider':
      return {}
    case 'aws':
      return {
        region: options?.aws_regions[0] || 'us-east-1',
        instance_type: options?.aws_instance_types[0] || 't3.micro',
        key_pair_name: '',
        security_group: '',
        ami_id: '',
        min_proxies: '1',
        max_proxies: '10',
        min_rotation_period_minutes: '60',
        max_rotation_period_minutes: '1440'
      }
    case 'gcp':
      return {
        region: options?.gcp_regions[0] || 'us-central1',
        zone: '',
        machine_type: options?.gcp_machine_types[0] || 'e2-micro',
        network: 'default',
        subnetwork: '',
        ssh_key: '',
        min_proxies: '1',
        max_proxies: '10',
        min_rotation_period_minutes: '60',
        max_rotation_period_minutes: '1440'
      }
    case 'azure':
      return {
        region: options?.azure_regions[0] || 'eastus',
        vm_size: options?.azure_vm_sizes[0] || 'Standard_B1s',
        resource_group: '',
        virtual_network: '',
        subnet: '',
        ssh_key_name: '',
        min_proxies: '1',
        max_proxies: '10',
        min_rotation_period_minutes: '60',
        max_rotation_period_minutes: '1440'
      }
    default:
      return {}
  }
}

const getDefaultCredentialConfig = (type: CredentialType): Record<string, string> => {
  switch (type) {
    case 'static_proxy_provider':
      return { username: '', password: '' }
    case 'aws':
      return { access_key: '', secret_key: '' }
    case 'gcp':
      return { service_account_json: '', project_id: '' }
    case 'azure':
      return { subscription_id: '', tenant_id: '', client_id: '', client_secret: '', key_vault_name: '' }
    default:
      return {}
  }
}

type WizardStep = 'select-type' | 'select-credential' | 'configure'

export default function ConnectorConfig() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()

  // Wizard state
  const [showWizard, setShowWizard] = useState(false)
  const [wizardStep, setWizardStep] = useState<WizardStep>('select-type')
  const [selectedType, setSelectedType] = useState<CredentialType | null>(null)

  // Form state
  const [formData, setFormData] = useState<ConnectorFormData>({ name: '', credential_id: '', config: {}, enabled: true })
  const [editingConnector, setEditingConnector] = useState<Connector | null>(null)

  // Credential creation modal state
  const [showCredentialModal, setShowCredentialModal] = useState(false)
  const [credentialFormData, setCredentialFormData] = useState<CredentialFormData>({ name: '', type: 'static_proxy_provider', config: {} })
  const [credentialModalError, setCredentialModalError] = useState<string | null>(null)

  // Error state for connector forms
  const [createError, setCreateError] = useState<string | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

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

  const createCredentialMutation = useMutation({
    mutationFn: (data: CredentialCreate) => createProjectCredential(selectedProjectId!, data),
    onSuccess: (newCredential) => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      setShowCredentialModal(false)
      setFormData({ ...formData, credential_id: newCredential.id })
      setCredentialFormData({ name: '', type: selectedType || 'static_proxy_provider', config: {} })
      setCredentialModalError(null)
    },
    onError: (error: Error) => {
      setCredentialModalError(error.message || 'Failed to create credential')
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: { name?: string; credential_id?: string; config?: Record<string, unknown>; enabled?: boolean } }) =>
      updateProjectConnector(selectedProjectId!, id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
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
  }

  const handleTypeSelect = (type: CredentialType) => {
    setSelectedType(type)
    setFormData({ ...formData, config: getDefaultConfig(type, optionsData) })
    setWizardStep('select-credential')
  }

  const handleCredentialSelect = (credentialId: string) => {
    if (credentialId === 'create-new') {
      setCredentialFormData({ name: '', type: selectedType!, config: getDefaultCredentialConfig(selectedType!) })
      setShowCredentialModal(true)
    } else {
      setFormData({ ...formData, credential_id: credentialId })
      setWizardStep('configure')
    }
  }

  const handleConfigChange = (key: string, value: string) => {
    setFormData({ ...formData, config: { ...formData.config, [key]: value } })
  }

  const handleCredentialConfigChange = (key: string, value: string) => {
    setCredentialFormData({ ...credentialFormData, config: { ...credentialFormData.config, [key]: value } })
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    createMutation.mutate({ name: formData.name, credential_id: formData.credential_id, config: formData.config, enabled: formData.enabled })
  }

  const handleCredentialSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    createCredentialMutation.mutate({ name: credentialFormData.name, type: credentialFormData.type, config: credentialFormData.config })
  }

  const getCredentialTypeLabel = (type: string | null) => {
    const labels: Record<string, string> = { static_proxy_provider: 'Static Proxy Provider', aws: 'AWS', gcp: 'GCP', azure: 'Azure' }
    return type ? labels[type] || type : 'Unknown'
  }

  const getCredentialsForType = () => {
    return credentialsData?.credentials.filter(c => c.type === selectedType) || []
  }

  const renderConfigFields = (type: CredentialType | null, config: Record<string, string>, onChange: (key: string, value: string) => void) => {
    if (!type || type === 'static_proxy_provider') return null
    const dropdownFields: Record<string, string[] | undefined> = {
      region: type === 'aws' ? optionsData?.aws_regions : type === 'gcp' ? optionsData?.gcp_regions : optionsData?.azure_regions,
      instance_type: optionsData?.aws_instance_types,
      machine_type: optionsData?.gcp_machine_types,
      vm_size: optionsData?.azure_vm_sizes,
    }
    const fields = getDefaultConfig(type, optionsData)
    return (
      <div className="grid grid-cols-2 gap-4 mt-4">
        {Object.keys(fields).map((key) => {
          const options = dropdownFields[key]
          const isNumber = ['min_proxies', 'max_proxies', 'min_rotation_period_minutes', 'max_rotation_period_minutes'].includes(key)
          return (
            <div key={key}>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                {key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}
              </label>
              {options ? (
                <select value={config[key] || ''} onChange={(e) => onChange(key, e.target.value)} className="w-full px-4 py-2 border rounded-lg">
                  {options.map((opt) => (<option key={opt} value={opt}>{opt}</option>))}
                </select>
              ) : (
                <input type={isNumber ? 'number' : 'text'} value={config[key] || ''} onChange={(e) => onChange(key, e.target.value)} className="w-full px-4 py-2 border rounded-lg" placeholder={key.replace(/_/g, ' ')} />
              )}
            </div>
          )
        })}
      </div>
    )
  }

  const renderCredentialConfigFields = () => {
    const type = credentialFormData.type
    const config = credentialFormData.config
    const fields = getDefaultCredentialConfig(type)
    return (
      <div className="grid gap-4 mt-4">
        {Object.keys(fields).map((key) => (
          <div key={key}>
            <label className="block text-sm font-medium text-gray-700 mb-1">{key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}</label>
            {key === 'service_account_json' ? (
              <textarea value={config[key] || ''} onChange={(e) => handleCredentialConfigChange(key, e.target.value)} className="w-full px-4 py-2 border rounded-lg h-32" placeholder="Paste service account JSON" />
            ) : (
              <input type={key.includes('password') || key.includes('secret') ? 'password' : 'text'} value={config[key] || ''} onChange={(e) => handleCredentialConfigChange(key, e.target.value)} className="w-full px-4 py-2 border rounded-lg" placeholder={key.replace(/_/g, ' ')} />
            )}
          </div>
        ))}
      </div>
    )
  }

  if (connectorsLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Connectors</h1>
        <button onClick={() => setShowWizard(true)} className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          <Link2 className="w-5 h-5" /> Add Connector
        </button>
      </div>

      {/* Wizard Modal */}
      {showWizard && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl max-w-4xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div className="p-6 border-b flex justify-between items-center">
              <div className="flex items-center gap-4">
                {wizardStep !== 'select-type' && (
                  <button onClick={() => setWizardStep(wizardStep === 'configure' ? 'select-credential' : 'select-type')} className="p-2 hover:bg-gray-100 rounded-lg">
                    <ArrowLeft className="w-5 h-5" />
                  </button>
                )}
                <h2 className="text-xl font-semibold">
                  {wizardStep === 'select-type' && 'Select Connector Type'}
                  {wizardStep === 'select-credential' && `Select ${getCredentialTypeLabel(selectedType)} Credential`}
                  {wizardStep === 'configure' && 'Configure Connector'}
                </h2>
              </div>
              <button onClick={resetWizard} className="p-2 hover:bg-gray-100 rounded-lg"><X className="w-5 h-5" /></button>
            </div>

            <div className="p-6">
              {/* Step 1: Select Type */}
              {wizardStep === 'select-type' && (
                <div className="grid grid-cols-2 gap-4">
                  {CONNECTOR_TYPES.map((ct) => (
                    <button key={ct.type} onClick={() => handleTypeSelect(ct.type)} className="flex items-center gap-4 p-6 border-2 rounded-xl hover:border-blue-500 hover:bg-blue-50 transition-all text-left">
                      <img src={ct.logo} alt={ct.name} className="w-16 h-16 object-contain" />
                      <div>
                        <h3 className="text-lg font-semibold">{ct.name}</h3>
                        <p className="text-gray-500 text-sm">{ct.description}</p>
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
                      <p className="text-gray-500 mb-4">No {getCredentialTypeLabel(selectedType)} credentials found.</p>
                      <button onClick={() => handleCredentialSelect('create-new')} className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 mx-auto">
                        <Plus className="w-5 h-5" /> Create New Credential
                      </button>
                    </div>
                  ) : (
                    <>
                      {getCredentialsForType().map((cred) => (
                        <button key={cred.id} onClick={() => handleCredentialSelect(cred.id)} className={`w-full flex items-center justify-between p-4 border-2 rounded-lg hover:border-blue-500 transition-all ${formData.credential_id === cred.id ? 'border-blue-500 bg-blue-50' : ''}`}>
                          <span className="font-medium">{cred.name}</span>
                          <span className="text-gray-500 text-sm">Created {new Date(cred.created_at).toLocaleDateString()}</span>
                        </button>
                      ))}
                      <button onClick={() => handleCredentialSelect('create-new')} className="w-full flex items-center justify-center gap-2 p-4 border-2 border-dashed rounded-lg hover:border-blue-500 hover:bg-blue-50 transition-all text-gray-600">
                        <Plus className="w-5 h-5" /> Create New Credential
                      </button>
                    </>
                  )}
                </div>
              )}

              {/* Step 3: Configure */}
              {wizardStep === 'configure' && (
                <form onSubmit={handleSubmit}>
                  {createError && (
                    <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
                      {createError}
                    </div>
                  )}
                  <div className="grid gap-4">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1">Connector Name</label>
                      <input type="text" value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} className="w-full px-4 py-2 border rounded-lg" placeholder="My Connector" required />
                    </div>
                    <label className="flex items-center gap-2">
                      <input type="checkbox" checked={formData.enabled} onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })} className="w-4 h-4" /> Enabled
                    </label>
                    {renderConfigFields(selectedType, formData.config, handleConfigChange)}
                  </div>
                  <div className="flex justify-end gap-4 mt-6">
                    <button type="button" onClick={resetWizard} className="px-4 py-2 border rounded-lg hover:bg-gray-50">Cancel</button>
                    <button type="submit" disabled={createMutation.isPending} className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
                      {createMutation.isPending ? 'Creating...' : 'Create Connector'}
                    </button>
                  </div>
                </form>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Credential Creation Modal */}
      {showCredentialModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
          <div className="bg-white rounded-lg shadow-xl max-w-lg w-full mx-4">
            <div className="p-6 border-b flex justify-between items-center">
              <h2 className="text-xl font-semibold">Create {getCredentialTypeLabel(selectedType)} Credential</h2>
              <button onClick={() => { setShowCredentialModal(false); setCredentialModalError(null) }} className="p-2 hover:bg-gray-100 rounded-lg"><X className="w-5 h-5" /></button>
            </div>
            <form onSubmit={handleCredentialSubmit} className="p-6">
              {credentialModalError && (
                <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg mb-4 flex justify-between items-center">
                  <span className="text-sm">{credentialModalError}</span>
                  <button type="button" onClick={() => setCredentialModalError(null)} className="text-red-500 hover:text-red-700">
                    <X className="w-4 h-4" />
                  </button>
                </div>
              )}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Credential Name</label>
                <input type="text" value={credentialFormData.name} onChange={(e) => setCredentialFormData({ ...credentialFormData, name: e.target.value })} className="w-full px-4 py-2 border rounded-lg" placeholder="My Credential" required />
              </div>
              {renderCredentialConfigFields()}
              <div className="flex justify-end gap-4 mt-6">
                <button type="button" onClick={() => { setShowCredentialModal(false); setCredentialModalError(null) }} className="px-4 py-2 border rounded-lg hover:bg-gray-50">Cancel</button>
                <button type="submit" disabled={createCredentialMutation.isPending} className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
                  {createCredentialMutation.isPending ? 'Creating...' : 'Create Credential'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Connector List */}
      <div className="grid gap-6">
        {connectorsData?.connectors.map((connector) => (
          <div key={connector.id} className="bg-white rounded-lg shadow p-6">
            {editingConnector?.id === connector.id ? (
              <form onSubmit={(e) => { e.preventDefault(); updateMutation.mutate({ id: connector.id, data: { name: editingConnector.name, enabled: editingConnector.enabled, config: editingConnector.config } }) }}>
                {editError && (
                  <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
                    {editError}
                  </div>
                )}
                <div className="space-y-4">
                  <div className="flex gap-4 items-center">
                    <div className="flex-1">
                      <label className="block text-sm font-medium text-gray-700 mb-1">Connector Name</label>
                      <input type="text" value={editingConnector.name} onChange={(e) => setEditingConnector({ ...editingConnector, name: e.target.value })} className="w-full px-4 py-2 border rounded-lg" required />
                    </div>
                    <label className="flex items-center gap-2 mt-6"><input type="checkbox" checked={editingConnector.enabled} onChange={(e) => setEditingConnector({ ...editingConnector, enabled: e.target.checked })} className="w-4 h-4" /> Enabled</label>
                  </div>
                  {renderConfigFields(
                    editingConnector.credential_type as CredentialType,
                    (editingConnector.config || {}) as Record<string, string>,
                    (key, value) => setEditingConnector({ ...editingConnector, config: { ...editingConnector.config, [key]: value } })
                  )}
                  <div className="flex gap-2 pt-4">
                    <button type="submit" className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700">Save</button>
                    <button type="button" onClick={() => { setEditingConnector(null); setEditError(null) }} className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300">Cancel</button>
                  </div>
                </div>
              </form>
            ) : (
              <div className="flex justify-between items-start">
                <div className="flex items-center gap-4">
                  <img src={CONNECTOR_TYPES.find(ct => ct.type === connector.credential_type)?.logo} alt="" className="w-10 h-10 object-contain" />
                  <div>
                    <h3 className="text-xl font-semibold">{connector.name}</h3>
                    <p className="text-gray-500 mt-1">Credential: {connector.credential_name || 'Unknown'} ({getCredentialTypeLabel(connector.credential_type)})</p>
                    <p className="text-gray-400 text-sm mt-1">Proxies: {connector.proxy_count}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`px-3 py-1 rounded-full text-sm font-medium ${connector.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}`}>{connector.enabled ? 'Enabled' : 'Disabled'}</span>
                  <button onClick={() => { setEditingConnector(connector); setEditError(null) }} className="p-2 text-gray-500 hover:text-blue-600"><Pencil className="w-5 h-5" /></button>
                  <button onClick={() => deleteMutation.mutate(connector.id)} className="p-2 text-gray-500 hover:text-red-600"><Trash2 className="w-5 h-5" /></button>
                </div>
              </div>
            )}
          </div>
        ))}

        {connectorsData?.connectors.length === 0 && (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <Link2 className="w-12 h-12 text-gray-400 mx-auto mb-4" />
            <h3 className="text-lg font-medium text-gray-900">No connectors configured</h3>
            <p className="text-gray-500 mt-2">Add a connector to start managing proxies from your credentials.</p>
          </div>
        )}
      </div>
    </div>
  )
}

