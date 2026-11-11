// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { BadgeCheck, KeyRound } from 'lucide-react'
import { saveLicense, type LicenseState, type LicenseStatus } from '../../api/client'
import { useToast } from '../../contexts/ToastContext'
import { isEnterprise, useEdition, useLicense } from '../../hooks/useEdition'
import { humanize } from '../../utils/audit'
import { HistoryButton } from '../audit/HistoryButton'
import { EnterpriseLinks } from '../EnterpriseCard'
import { Alert, Badge, Button, Card, CardHeader, Label, LogoLoader, QueryContent, Textarea } from '../ui'

const STATES = new Map<LicenseState, { label: string; color: 'green' | 'yellow' | 'red' | 'gray' }>([
  ['active', { label: 'Active', color: 'green' }],
  ['grace', { label: 'Grace period', color: 'yellow' }],
  ['expired', { label: 'Expired', color: 'red' }],
  ['invalid', { label: 'Invalid', color: 'red' }],
  ['none', { label: 'No key', color: 'gray' }],
])

// Feature names come from the license itself; one this release does not know reads as its name.
const FEATURES = new Map([['audit', 'Audit log']])

/** A license's day (`YYYY-MM-DD`, UTC) the way the viewer writes dates, the same day wherever they are. */
function formatDay(day: string | null): string {
  if (!day) return '-'
  const date = new Date(`${day}T00:00:00Z`)
  return Number.isNaN(date.getTime()) ? day : date.toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' })
}

/** The System page's first card: the edition on the public image, the license with its key on the Enterprise one. */
export function LicenseCard() {
  const editionQuery = useEdition()
  return (
    <QueryContent query={editionQuery} what="the edition" loader={<LogoLoader className="py-6" />}>
      {(edition) => (isEnterprise(edition) ? <EnterpriseLicense /> : <FreeEdition />)}
    </QueryContent>
  )
}

function FreeEdition() {
  return (
    <Card className="p-5">
      <CardHeader title={<span className="inline-flex items-center gap-2"><BadgeCheck className="w-4 h-4 text-fg-subtle" /> Free edition</span>} />
      <p className="text-sm text-fg-muted mt-1">
        This install runs the free edition of Octoprox. Octoprox Enterprise adds every sign-in and API token to the audit log, with the whole install's activity on one page. It runs from its own image, built on the same release, and its license key is entered here.
      </p>
      <EnterpriseLinks className="mt-3" />
    </Card>
  )
}

function EnterpriseLicense() {
  const licenseQuery = useLicense()
  return (
    <Card className="p-5">
      <QueryContent query={licenseQuery} what="the license" loader={<LogoLoader className="py-6" />}>
        {(license) => <LicenseDetails license={license} />}
      </QueryContent>
    </Card>
  )
}

function LicenseDetails({ license }: { license: LicenseStatus }) {
  const state = STATES.get(license.state) ?? { label: humanize(license.state), color: 'gray' as const }
  const genuine = license.state === 'active' || license.state === 'grace' || license.state === 'expired'
  // More users than seats blocks nobody: it shows here and in the banner.
  const overSeats = license.seats !== null && license.seats_used > license.seats
  return (
    <>
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><KeyRound className="w-4 h-4 text-fg-subtle" /> License</span>}
        action={
          <div className="flex items-center gap-2">
            <Badge color={state.color}>{state.label}</Badge>
            <HistoryButton entityType="system_settings" entityId="1" />
          </div>
        }
      />
      {license.state === 'none' && (
        <p className="text-sm text-fg-muted mt-1">No license key is stored. Paste the key you were sent to turn on install-wide activity, with sign-ins and tokens recorded.</p>
      )}
      {license.state === 'invalid' && (
        <Alert variant="error" className="mt-3 wrap-break-word">The stored key is refused: {license.refusal ?? 'it is not an Octoprox license key'}.</Alert>
      )}
      {genuine && (
        <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-fg-muted">Organisation</dt>
          <dd className="wrap-break-word">{license.organisation ?? '-'}</dd>
          <dt className="text-fg-muted">License</dt>
          <dd className="font-mono text-xs wrap-break-word self-center">{license.license_id ?? '-'}</dd>
          <dt className="text-fg-muted">Expires</dt>
          <dd className={license.state === 'expired' ? 'text-danger' : undefined}>{formatDay(license.expires)}</dd>
          <dt className="text-fg-muted">Grace ends</dt>
          <dd className={license.state === 'grace' ? 'text-warning' : license.state === 'expired' ? 'text-danger' : undefined}>{formatDay(license.grace_ends)}</dd>
          <dt className="text-fg-muted">Seats</dt>
          <dd className={overSeats ? 'text-danger font-medium' : undefined}>
            {license.seats === null ? `${license.seats_used} in use` : `${license.seats_used} of ${license.seats} seats`}
          </dd>
          <dt className="text-fg-muted">Features</dt>
          <dd className="flex flex-wrap gap-1">
            {license.features.length === 0 ? '-' : license.features.map((f) => <Badge key={f} color="blue" className="py-0.5">{FEATURES.get(f) ?? humanize(f)}</Badge>)}
          </dd>
        </dl>
      )}
      <KeyForm license={license} />
    </>
  )
}

/** Paste a key: the api checks it and says why when it refuses it. */
function KeyForm({ license }: { license: LicenseStatus }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [key, setKey] = useState('')
  const save = useMutation({
    mutationFn: () => saveLicense(key.trim()),
    onSuccess: (status) => {
      queryClient.setQueryData(['license'], status)
      // The login page and the edition name the licensed organisation.
      queryClient.invalidateQueries({ queryKey: ['edition'] })
      setKey('')
      toast.show('License key saved')
    },
  })
  return (
    <form className="mt-4 pt-4 border-t border-line space-y-2" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
      <Label htmlFor="license-key">{license.state === 'none' ? 'License key' : 'Replace the license key'}</Label>
      <Textarea
        id="license-key"
        value={key}
        onChange={(e) => { setKey(e.target.value); if (save.error) save.reset() }}
        rows={3}
        spellCheck={false}
        autoComplete="off"
        autoCapitalize="off"
        placeholder="oxl1…"
        className="font-mono text-base pointer-fine:text-xs break-all"
      />
      {save.error && <Alert variant="error" className="wrap-break-word">{save.error.message || 'The key could not be saved'}</Alert>}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <span className="text-[11px] text-fg-subtle">Checked offline: nothing is sent anywhere.</span>
        <Button type="submit" size="sm" disabled={!key.trim() || save.isPending} className="ml-auto pointer-coarse:h-11">
          {save.isPending ? 'Checking…' : 'Save key'}
        </Button>
      </div>
    </form>
  )
}
