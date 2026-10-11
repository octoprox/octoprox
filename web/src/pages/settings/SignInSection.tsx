// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ExternalLink, Lock } from 'lucide-react'
import { fetchAuthProviders, updateAuthProvider, AuthProviderDoc, AuthProviderSettings } from '../../api/client'
import { useToast } from '../../contexts/ToastContext'
import { Page } from '../../components/layout/Page'
import { ProviderMark } from '../../components/SignInProviderMark'
import { formatDateTime } from '../../utils/format'
import { Alert, Badge, Button, Card, CardHeader, InfoTip, Input, Label } from '../../components/ui'

const DOCS_URL = 'https://octoprox.github.io/octoprox/sso'

const PROVIDER_HELP: Record<AuthProviderSettings['kind'], { where: string; href: string }> = {
  google: { where: 'Google Cloud Console, APIs & Services, Credentials: an OAuth client of type Web application', href: 'https://console.cloud.google.com/apis/credentials' },
  microsoft: { where: 'Microsoft Entra admin center, App registrations: a Web platform with the redirect URI below', href: 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade' },
  github: { where: 'GitHub, Settings, Developer settings, OAuth Apps (or your organisation\'s)', href: 'https://github.com/settings/developers' },
}

/**
 * Admin page for sign-in through Google, Microsoft or GitHub. Each provider
 * is one OAuth client this install registers with it; signing in matches the
 * provider's verified email against the users the admin has added.
 */
export default function SignInSection() {
  const { data: providers, isLoading } = useQuery({ queryKey: ['auth-providers'], queryFn: fetchAuthProviders })

  return (
    <Page
      title="Sign-in"
      subtitle="Let the people you have added sign in with a Google, Microsoft or GitHub account instead of a password."
    >
      {isLoading || !providers ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : (
        <div className="grid gap-4 @4xl:grid-cols-[minmax(0,1fr)_380px]">
          <div className="space-y-4">
            <Alert variant="info" className="text-sm">
              Nobody self-registers. A sign-in succeeds only when the provider vouches for an email address that belongs to an existing, active user. Add people under Users first, with the email they use at the provider.
            </Alert>
            {providers.map((p) => <ProviderCard key={p.kind} settings={p} />)}
          </div>
          <EnterpriseCard />
        </div>
      )}
    </Page>
  )
}

function ProviderCard({ settings }: { settings: AuthProviderSettings }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const toDoc = (s: AuthProviderSettings): AuthProviderDoc => ({ enabled: s.enabled, client_id: s.client_id, client_secret: null, tenant_id: s.tenant_id })
  const [doc, setDoc] = useState<AuthProviderDoc>(toDoc(settings))
  const saved = JSON.stringify(toDoc(settings))
  useEffect(() => setDoc(JSON.parse(saved) as AuthProviderDoc), [saved])
  const dirty = JSON.stringify(doc) !== saved
  const help = PROVIDER_HELP[settings.kind]

  const save = useMutation({
    mutationFn: () => updateAuthProvider(settings.kind, doc),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['auth-providers'] })
      queryClient.invalidateQueries({ queryKey: ['sso-providers'] })
      toast.show(`${settings.label} sign-in saved`)
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to save', 'error'),
  })

  return (
    <Card className="p-5">
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><ProviderMark kind={settings.kind} className="w-4 h-4" /> {settings.label}</span>}
        action={settings.enabled ? <Badge color="green">On</Badge> : settings.configured ? <Badge color="gray">Off</Badge> : <Badge color="gray">Not set up</Badge>}
      />
      <p className="text-xs text-fg-muted mt-1">
        Register an OAuth client in {help.where}
        {' '}<a href={help.href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-primary hover:underline">open <ExternalLink className="w-3 h-3" /></a>, give it the redirect URI below, and paste its client ID and secret here.
      </p>
      <form className="mt-4 grid grid-cols-1 @lg:grid-cols-2 gap-4" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
        <div className="@lg:col-span-2">
          <Label className="inline-flex items-center gap-1">Redirect URI <InfoTip>What the provider must be allowed to send the browser back to. It is built from the public URL (OCTOPROX_PUBLIC_URL); if it shows an internal address, set that first.</InfoTip></Label>
          <CopyField value={settings.redirect_uri} />
        </div>
        <div>
          <Label htmlFor={`${settings.kind}-client-id`}>Client ID</Label>
          <Input id={`${settings.kind}-client-id`} value={doc.client_id} onChange={(e) => setDoc({ ...doc, client_id: e.target.value })} autoComplete="off" className="font-mono text-xs" />
        </div>
        <div>
          <Label htmlFor={`${settings.kind}-client-secret`}>Client secret</Label>
          <Input
            id={`${settings.kind}-client-secret`}
            type="password"
            value={doc.client_secret ?? ''}
            onChange={(e) => setDoc({ ...doc, client_secret: e.target.value === '' ? null : e.target.value })}
            placeholder={settings.has_client_secret ? 'Stored; type to replace' : 'Required'}
            autoComplete="new-password"
            className="font-mono text-xs"
          />
        </div>
        {settings.kind === 'microsoft' && (
          <div className="@lg:col-span-2">
            <Label htmlFor="microsoft-tenant" className="inline-flex items-center gap-1">Tenant ID <InfoTip>The directory (tenant) ID of the Entra tenant whose accounts may sign in. Required: 'common' would admit any Microsoft account in the world, and only within one tenant are email addresses set by that tenant's admins.</InfoTip></Label>
            <Input id="microsoft-tenant" value={doc.tenant_id ?? ''} onChange={(e) => setDoc({ ...doc, tenant_id: e.target.value })} placeholder="00000000-0000-0000-0000-000000000000" autoComplete="off" className="font-mono text-xs" />
          </div>
        )}
        <div className="@lg:col-span-2 flex items-center justify-between gap-3 pt-1">
          <label className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={doc.enabled} onChange={(e) => setDoc({ ...doc, enabled: e.target.checked })} className="w-4 h-4" />
            Show "Continue with {settings.label}" on the login page
          </label>
          <div className="flex items-center gap-3">
            {settings.updated_by && <span className="text-[11px] text-fg-subtle">Saved by {settings.updated_by}, {formatDateTime(settings.updated_at)}</span>}
            <Button type="submit" size="sm" disabled={!dirty || save.isPending}>Save</Button>
          </div>
        </div>
      </form>
    </Card>
  )
}

function CopyField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(value) } catch { /* the field is selectable */ }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <div className="flex items-stretch gap-2">
      <code className="flex-1 min-w-0 font-mono text-xs bg-surface-raised rounded-lg px-3 py-2 break-all select-all flex items-center">{value}</code>
      <Button type="button" variant="outline" size="sm" onClick={copy} className="flex-none">
        {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
      </Button>
    </div>
  )
}

function EnterpriseCard() {
  const items = [
    ['Domain rules', 'Everyone at @yourcompany.com signs in and gets an account on first login; no per-user step.'],
    ['Your own IdP', 'Okta, Entra ID, Auth0, Keycloak, Ping or any OpenID Connect or SAML 2.0 provider.'],
    ['Group sync', 'Admin, editor and viewer from directory groups, re-evaluated on every sign-in.'],
    ['Enforcement', 'Require the IdP for everyone except break-glass admins; disable passwords.'],
    ['Provisioning', 'SCIM 2.0: leavers lose access the moment the directory says so.'],
    ['Audit log', 'Every sign-in, token and provider change, exportable to your SIEM.'],
  ]
  return (
    <Card className="p-5 h-fit">
      <CardHeader title={<span className="inline-flex items-center gap-2"><Lock className="w-4 h-4 text-fg-subtle" /> Enterprise SSO</span>} action={<Badge color="blue">License</Badge>} />
      <p className="text-xs text-fg-muted mt-1">
        The sign-in above is for teams whose admin adds each person. Octoprox Enterprise hands access control to your directory instead. It is enabled by a license key under System.
      </p>
      <ul className="mt-3 space-y-2">
        {items.map(([title, text]) => (
          <li key={title} className="text-xs">
            <span className="font-medium text-fg">{title}.</span> <span className="text-fg-muted">{text}</span>
          </li>
        ))}
      </ul>
      <a href={DOCS_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-primary hover:underline mt-4">
        How it works and how to get a license <ExternalLink className="w-3 h-3" />
      </a>
    </Card>
  )
}
