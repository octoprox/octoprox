// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, Upload } from 'lucide-react'
import { exportBackup, importBackup, ImportSummary, logout } from '../../api/client'
import { Page } from '../../components/layout/Page'
import { Alert, Button, Card, Input, Label, ConfirmDialog } from '../../components/ui'

const MIN_PASSPHRASE = 8

export default function BackupSection() {
  return (
    <Page
      title="Backup & Migration"
      subtitle="Export the whole instance — users, projects, credentials, connectors and proxies — as one encrypted file, or restore one to migrate."
    >
      <div className="grid grid-cols-2 gap-4 max-w-5xl items-stretch">
        <ExportCard />
        <ImportCard />
      </div>
    </Page>
  )
}

function ExportCard() {
  const [passphrase, setPassphrase] = useState('')
  const [confirm, setConfirm] = useState('')
  const [includeMetrics, setIncludeMetrics] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => exportBackup(passphrase, includeMetrics),
    onSuccess: () => { setError(null); setPassphrase(''); setConfirm('') },
    onError: (err: any) => setError(err.message || 'Export failed'),
  })

  const handleExport = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (passphrase.length < MIN_PASSPHRASE) { setError(`Passphrase must be at least ${MIN_PASSPHRASE} characters.`); return }
    if (passphrase !== confirm) { setError('Passphrases do not match.'); return }
    mutation.mutate()
  }

  return (
    <Card className="p-5 flex flex-col">
      <div className="flex items-center gap-2 mb-1">
        <Download className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">Export</h3>
      </div>
      <p className="text-xs text-fg-muted mb-4">
        The file is encrypted with the passphrase you choose. You need the same passphrase to import it; it cannot be recovered if lost.
      </p>
      <form onSubmit={handleExport} className="flex-1 flex flex-col gap-3">
        {error && <Alert variant="error">{error}</Alert>}
        <div>
          <Label htmlFor="export-passphrase">Passphrase</Label>
          <Input id="export-passphrase" type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder={`At least ${MIN_PASSPHRASE} characters`} autoComplete="new-password" />
        </div>
        <div>
          <Label htmlFor="export-passphrase-confirm">Confirm passphrase</Label>
          <Input id="export-passphrase-confirm" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="Repeat passphrase" autoComplete="new-password" />
        </div>
        <label className="flex items-center gap-2 text-[13px] text-fg">
          <input type="checkbox" checked={includeMetrics} onChange={(e) => setIncludeMetrics(e.target.checked)} />
          Include historical metrics (larger file)
        </label>
        <div className="flex justify-end mt-auto pt-1">
          <Button type="submit" size="sm" disabled={mutation.isPending}>{mutation.isPending ? 'Exporting…' : 'Export backup'}</Button>
        </div>
      </form>
    </Card>
  )
}

function ImportCard() {
  const queryClient = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const [keepCurrentUser, setKeepCurrentUser] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [summary, setSummary] = useState<ImportSummary | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const mutation = useMutation({
    mutationFn: () => importBackup(file as File, passphrase, keepCurrentUser),
    onSuccess: (data) => {
      setError(null)
      setSummary(data)
      setConfirmOpen(false)
      if (data.kept_current_user) {
        queryClient.invalidateQueries()
      } else {
        // The current session references a user that no longer exists after
        // the replace, so force a re-login once the admin has seen the summary.
        setTimeout(() => logout(), 4000)
      }
    },
    onError: (err: any) => { setError(err.message || 'Import failed'); setConfirmOpen(false) },
  })

  const canImport = !!file && passphrase.length >= MIN_PASSPHRASE

  return (
    <Card className="p-5 flex flex-col">
      <div className="flex items-center gap-2 mb-1">
        <Upload className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">Restore</h3>
      </div>
      <p className="text-xs text-fg-muted mb-4">
        Restore a backup file. This <strong className="text-fg">replaces all existing data</strong> on this instance.
      </p>
      <div className="flex-1 flex flex-col gap-3">
        {error && <Alert variant="error">{error}</Alert>}
        {summary && (
          <Alert variant="success">
            <p>
              Import complete: {summary.projects} projects, {summary.credentials} credentials, {summary.connectors} connectors, {summary.proxies} proxies, {summary.users} users restored.{' '}
              {summary.kept_current_user ? 'Your account was kept and you remain signed in.' : 'You will be signed out — please log in again.'}
            </p>
            {summary.user_conflicts.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-xs">
                {summary.user_conflicts.map((c) => (
                  <li key={c.original_username}>
                    Imported user <strong>{c.original_username}</strong> clashed with your account
                    {c.new_username !== c.original_username && <> and was renamed to <strong>{c.new_username}</strong></>}
                    {c.email_cleared && <>; its email was cleared</>}.
                  </li>
                ))}
              </ul>
            )}
          </Alert>
        )}
        <label className="block border border-dashed border-line-strong rounded-[10px] p-4 text-center cursor-pointer hover:border-primary hover:bg-primary-soft/40 transition-colors">
          <Upload className="w-5 h-5 mx-auto text-fg-subtle mb-1.5" />
          <span className="block text-sm">{file ? <span className="font-medium text-fg">{file.name}</span> : <><span className="font-medium text-fg">Choose a backup file</span> <span className="text-fg-muted">(.opbak)</span></>}</span>
          <input
            id="import-file"
            ref={fileInputRef}
            type="file"
            accept=".opbak,application/octet-stream"
            className="sr-only"
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setSummary(null) }}
          />
        </label>
        <div>
          <Label htmlFor="import-passphrase">Passphrase</Label>
          <Input id="import-passphrase" type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder="Passphrase used to create the backup" autoComplete="off" />
        </div>
        <label className="flex items-start gap-2 text-[13px] text-fg">
          <input type="checkbox" className="mt-0.5" checked={keepCurrentUser} onChange={(e) => setKeepCurrentUser(e.target.checked)} />
          <span>
            Keep my current account
            <span className="block text-xs text-fg-muted">
              Your user is preserved so you stay signed in. An imported user with the same username is renamed (suffix <code>-imported</code>) and a clashing email is cleared. Untick to restore users exactly as they are in the backup.
            </span>
          </span>
        </label>
        <div className="flex justify-end mt-auto pt-1">
          <Button type="button" variant="danger" size="sm" disabled={!canImport || mutation.isPending} onClick={() => { setError(null); setConfirmOpen(true) }}>
            Import &amp; replace
          </Button>
        </div>
      </div>

      {confirmOpen && (
        <ConfirmDialog
          title="Replace all data?"
          message={<>This permanently deletes every project, credential, connector, proxy and user{keepCurrentUser ? ' (except your own account)' : ''} on this instance and replaces them with the backup. This cannot be undone.</>}
          confirmLabel="Yes, replace everything"
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => mutation.mutate()}
          isLoading={mutation.isPending}
        />
      )}
    </Card>
  )
}
