import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const cardClasses = 'bg-white dark:bg-gray-800 rounded-lg shadow'

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn(cardClasses, className)} {...props} />
}
