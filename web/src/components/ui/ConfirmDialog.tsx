// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Modal, ModalFooter } from './Modal'
import { Button } from './Button'

/**
 * The one place a modal is still the right tool: confirming a destructive
 * action. Everything else lives in the docked Inspector.
 */
interface ConfirmDialogProps {
  title: string
  message: ReactNode
  confirmLabel?: string
  onConfirm: () => void
  onCancel: () => void
  isLoading?: boolean
  danger?: boolean
  children?: ReactNode
  confirmDisabled?: boolean
}

export function ConfirmDialog({ title, message, confirmLabel = 'Delete', onConfirm, onCancel, isLoading, danger = true, children, confirmDisabled }: ConfirmDialogProps) {
  return (
    <Modal onClose={onCancel} className="p-5 max-w-md">
      <div className="flex items-start gap-3">
        {danger && (
          <div className="w-9 h-9 rounded-full bg-danger-soft text-danger flex items-center justify-center flex-none">
            <AlertTriangle className="w-4 h-4" />
          </div>
        )}
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold text-fg">{title}</h2>
          <div className="text-sm text-fg-muted mt-1">{message}</div>
          {children && <div className="mt-3">{children}</div>}
        </div>
      </div>
      <ModalFooter className="mt-5">
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>Cancel</Button>
        <Button type="button" variant={danger ? 'danger' : 'primary'} size="sm" onClick={onConfirm} disabled={isLoading || confirmDisabled}>
          {isLoading ? 'Working…' : confirmLabel}
        </Button>
      </ModalFooter>
    </Modal>
  )
}
