import { type HTMLAttributes, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '../../utils/cn'

interface ModalProps {
  children: ReactNode
  onClose: () => void
  className?: string
  overlayClassName?: string
}

export function Modal({ children, onClose, className, overlayClassName }: ModalProps) {
  return (
    <div
      className={cn(
        'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4',
        overlayClassName
      )}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className={cn(
          'bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md',
          className
        )}
      >
        {children}
      </div>
    </div>
  )
}

interface ModalHeaderProps {
  title: string
  onClose: () => void
  children?: ReactNode
}

export function ModalHeader({ title, onClose, children }: ModalHeaderProps) {
  return (
    <div className="flex justify-between items-center mb-4">
      <div className="flex items-center gap-4">
        {children}
        <h2 className="text-xl font-semibold">{title}</h2>
      </div>
      <button
        type="button"
        onClick={onClose}
        className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
      >
        <X className="w-5 h-5" />
      </button>
    </div>
  )
}

export function ModalFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex justify-end gap-3 mt-6', className)} {...props} />
}
