// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { Check, AlertCircle } from 'lucide-react'

interface Toast {
  id: number
  message: string
  variant: 'success' | 'error'
}

interface ToastContextValue {
  show: (message: string, variant?: Toast['variant']) => void
}

const ToastContext = createContext<ToastContextValue>({ show: () => {} })

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const seq = useRef(0)

  const show = useCallback((message: string, variant: Toast['variant'] = 'success') => {
    const id = ++seq.current
    setToasts((t) => [...t, { id, message, variant }])
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), variant === 'error' ? 5000 : 2500)
  }, [])

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="fixed left-1/2 bottom-6 -translate-x-1/2 z-[70] flex flex-col items-center gap-2 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-lg shadow-lg text-sm bg-fg text-bg animate-toast-in"
            role="status"
          >
            {t.variant === 'success'
              ? <Check className="w-4 h-4 text-success" />
              : <AlertCircle className="w-4 h-4 text-danger" />}
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  return useContext(ToastContext)
}
