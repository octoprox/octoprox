// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { ExternalLink, Lock, Mail } from 'lucide-react'
import { cn } from '../utils/cn'
import { DOCS_URL } from '../utils/docs'
import { Badge, Card, CardHeader } from './ui'

export interface EnterpriseFeature {
  title: string
  text: string
  /** Not in Octoprox Enterprise yet: marked as coming in a later release. */
  later?: boolean
}

/** What Octoprox Enterprise adds to a page of the free edition, where to read about it and how to ask for it. */
export function EnterpriseCard({ title, intro, features }: {
  title: ReactNode
  intro: ReactNode
  features: EnterpriseFeature[]
}) {
  return (
    <Card className="p-5 h-fit">
      <CardHeader title={<span className="inline-flex items-center gap-2"><Lock className="w-4 h-4 text-fg-subtle" /> {title}</span>} action={<Badge color="blue">License</Badge>} />
      <p className="text-xs text-fg-muted mt-1">{intro}</p>
      <ul className="mt-3 space-y-2">
        {features.map((feature) => (
          <li key={feature.title} className="text-xs">
            <span className="font-medium text-fg">{feature.title}.</span> <span className="text-fg-muted">{feature.text}</span>
            {feature.later && <Badge color="gray" className="ml-1.5 py-0 text-[10px] whitespace-nowrap">Later release</Badge>}
          </li>
        ))}
      </ul>
      <EnterpriseLinks className="mt-4" />
    </Card>
  )
}

/** Get in touch (the website's contact section) and Learn more (its Octoprox Enterprise page), each in a new tab: 44 px targets on touch screens. */
export function EnterpriseLinks({ className }: { className?: string }) {
  return (
    <div className={cn('flex flex-wrap items-center gap-x-4 gap-y-2', className)}>
      <a
        href={`${DOCS_URL}/enterprise#get-in-touch`}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-line-strong bg-surface text-[13px] font-medium text-fg hover:bg-surface-raised transition-colors pointer-coarse:h-11"
      >
        <Mail className="w-3.5 h-3.5" /> Get in touch
      </a>
      <a href={`${DOCS_URL}/enterprise`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-primary hover:underline pointer-coarse:min-h-11">
        Learn more <ExternalLink className="w-3 h-3" />
      </a>
    </div>
  )
}
