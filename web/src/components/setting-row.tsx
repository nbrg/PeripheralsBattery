import type { ReactNode } from "react"

import { Label } from "@/components/ui/label"

/** One labelled setting: text on the left, the control on the right. */
export function SettingRow({ id, title, description, children }: {
  id: string
  title: string
  description?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-6 py-3.5">
      <div className="grid gap-1">
        <Label htmlFor={id} className="text-sm font-medium">{title}</Label>
        {description && <p className="text-muted-foreground text-[13px] leading-snug">{description}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

export function PageHeader({ title, description, actions }: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="mb-6 flex items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="text-muted-foreground mt-1 text-sm">{description}</p>}
      </div>
      {actions}
    </div>
  )
}
