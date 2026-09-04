// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Download, Upload } from 'lucide-react'
import { exportBackup, importBackup, ImportSummary, logout } from '../../api/client'
import { Alert, Button, Card, Input, Label, Modal, ModalFooter, ModalHeader } from '../../components/ui'

const MIN_PASSPHRASE = 8

export default function BackupSection() {
  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-xl font-semibold text-fg">Backup &amp; Migration</h2>
        <p className="text-sm text-fg-muted mt-1">
          Export your entire setup — users, projects, credentials, connectors and proxies — as a
          single encrypted file, or restore one to migrate to another instance.
        </p>
      </header>

      <ExportCard />
      <ImportCard />
    </div>
  )
}

function ExportCard() {
  const [passphrase, setPassphrase] = useState('')
  const [confirm, setConfirm] = useState('')
  const [includeMetrics, setIncludeMetrics] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => exportBackup(passphrase, includeMetrics),
    onSuccess: () => {
      setError(null)
      setPassphrase('')
      setConfirm('')
    },
    onError: (err: any) => setError(err.message || 'Export failed'),
  })

  const handleExport = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (passphrase.length < MIN_PASSPHRASE) {
      setError(`Passphrase must be at least ${MIN_PASSPHRASE} characters.`)
      return
    }
    if (passphrase !== confirm) {
      setError('Passphrases do not match.')
      return
    }
    mutation.mutate()
  }

  return (
    <Card className="p-6">
      <div className="flex items-center gap-2 mb-1">
        <Download className="w-4 h-4 text-fg-muted" />
        <h3 className="font-semibold text-fg">Export</h3>
      </div>
      <p className="text-sm text-fg-muted mb-4">
        The file is encrypted with the passphrase you choose. You will need the same passphrase to
        import it — it cannot be recovered if lost.
      </p>
      <form onSubmit={handleExport} className="space-y-4">
        {error && <Alert variant="error">{error}</Alert>}
        <div>
          <Label htmlFor="export-passphrase">Passphrase</Label>
          <Input
            id="export-passphrase"
            type="password"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            placeholder={`At least ${MIN_PASSPHRASE} characters`}
            autoComplete="new-password"
          />
        </div>
        <div>
          <Label htmlFor="export-passphrase-confirm">Confirm passphrase</Label>
          <Input
            id="export-passphrase-confirm"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="Repeat passphrase"
            autoComplete="new-password"
          />
        </div>
        <label className="flex items-center gap-2 text-sm text-fg">
          <input
            type="checkbox"
            checked={includeMetrics}
            onChange={(e) => setIncludeMetrics(e.target.checked)}
          />
          Include historical metrics (larger file)
        </label>
        <div className="flex justify-end">
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? 'Exporting…' : 'Export backup'}
          </Button>
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
        // Our account survived; every other cached entity is now stale.
        queryClient.invalidateQueries()
      } else {
        // The current session references a user that no longer exists after
        // the replace, so force a re-login once the admin has seen the summary.
        setTimeout(() => logout(), 4000)
      }
    },
    onError: (err: any) => {
      setError(err.message || 'Import failed')
      setConfirmOpen(false)
    },
  })

  const canImport = !!file && passphrase.length >= MIN_PASSPHRASE

  return (
    <Card className="p-6">
      <div className="flex items-center gap-2 mb-1">
        <Upload className="w-4 h-4 text-fg-muted" />
        <h3 className="font-semibold text-fg">Import</h3>
      </div>
      <p className="text-sm text-fg-muted mb-4">
        Restore a backup file. This <strong>replaces all existing data</strong> on this instance.
      </p>

      <div className="space-y-4">
        {error && <Alert variant="error">{error}</Alert>}
        {summary && (
          <Alert variant="success">
            <p>
              Import complete: {summary.projects} projects, {summary.credentials} credentials,{' '}
              {summary.connectors} connectors, {summary.proxies} proxies, {summary.users} users
              restored.{' '}
              {summary.kept_current_user
                ? 'Your account was kept and you remain signed in.'
                : 'You will be signed out — please log in again.'}
            </p>
            {summary.user_conflicts.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-sm">
                {summary.user_conflicts.map((c) => (
                  <li key={c.original_username}>
                    Imported user <strong>{c.original_username}</strong> clashed with your account
                    {c.new_username !== c.original_username && (
                      <> and was renamed to <strong>{c.new_username}</strong></>
                    )}
                    {c.email_cleared && <>; its email was cleared</>}.
                  </li>
                ))}
              </ul>
            )}
          </Alert>
        )}
        <div>
          <Label htmlFor="import-file">Backup file</Label>
          <input
            id="import-file"
            ref={fileInputRef}
            type="file"
            accept=".opbak,application/octet-stream"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null)
              setSummary(null)
            }}
            className="block w-full text-sm text-fg-muted file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:bg-surface-raised file:text-fg hover:file:bg-line"
          />
        </div>
        <div>
          <Label htmlFor="import-passphrase">Passphrase</Label>
          <Input
            id="import-passphrase"
            type="password"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            placeholder="Passphrase used to create the backup"
            autoComplete="off"
          />
        </div>
        <label className="flex items-start gap-2 text-sm text-fg">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={keepCurrentUser}
            onChange={(e) => setKeepCurrentUser(e.target.checked)}
          />
          <span>
            Keep my current account
            <span className="block text-fg-muted">
              Your user is preserved so you stay signed in. An imported user with the same
              username is renamed (suffix <code>-imported</code>) and a clashing email is
              cleared. Untick to restore users exactly as they are in the backup.
            </span>
          </span>
        </label>
        <div className="flex justify-end">
          <Button
            type="button"
            variant="danger"
            disabled={!canImport || mutation.isPending}
            onClick={() => {
              setError(null)
              setConfirmOpen(true)
            }}
          >
            Import &amp; replace
          </Button>
        </div>
      </div>

      {confirmOpen && (
        <Modal onClose={() => setConfirmOpen(false)}>
          <div className="p-6">
            <ModalHeader title="Replace all data?" onClose={() => setConfirmOpen(false)}>
              <AlertTriangle className="w-5 h-5 text-danger" />
            </ModalHeader>
            <p className="text-sm text-fg-muted">
              This will permanently delete every project, credential, connector, proxy and user
              {keepCurrentUser ? ' (except your own account)' : ''} on this instance and replace
              them with the contents of the backup file. This cannot be undone.
            </p>
            <ModalFooter>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                Cancel
              </Button>
              <Button
                variant="danger"
                disabled={mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                {mutation.isPending ? 'Importing…' : 'Yes, replace everything'}
              </Button>
            </ModalFooter>
          </div>
        </Modal>
      )}
    </Card>
  )
}
