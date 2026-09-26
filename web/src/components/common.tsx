import type { ReactNode } from "react"
import {
  Body1,
  Caption1,
  Card,
  Label,
  Subtitle2,
  Title2,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components"

import { BATTERY } from "./theme"

const useStyles = makeStyles({
  header: { display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: "16px", marginBottom: "24px" },
  headerText: { display: "grid", gap: "4px" },
  sub: { color: tokens.colorNeutralForeground3 },
  section: { display: "grid", gap: "0", padding: "20px 24px" },
  sectionHead: { display: "grid", gap: "2px", marginBottom: "8px" },
  row: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "24px",
    padding: "14px 0",
    borderTop: `1px solid ${tokens.colorNeutralStroke2}`,
    ":first-of-type": { borderTop: "none" },
  },
  rowText: { display: "grid", gap: "2px" },
  track: {
    height: "8px",
    width: "100%",
    borderRadius: tokens.borderRadiusCircular,
    backgroundColor: tokens.colorNeutralBackground5,
    overflow: "hidden",
  },
  fill: { height: "100%", borderRadius: tokens.borderRadiusCircular, transition: "width 500ms ease" },
  pulse: {
    animationName: { from: { opacity: 1 }, "50%": { opacity: 0.55 }, to: { opacity: 1 } },
    animationDuration: "1.8s",
    animationIterationCount: "infinite",
  },
})

export function PageHeader({ title, description, actions }: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  const s = useStyles()
  return (
    <header className={s.header}>
      <div className={s.headerText}>
        <Title2 as="h1" style={{ margin: 0 }}>{title}</Title2>
        {description && <Body1 className={s.sub}>{description}</Body1>}
      </div>
      {actions}
    </header>
  )
}

export function Section({ title, description, children }: {
  title: string
  description?: string
  children: ReactNode
}) {
  const s = useStyles()
  return (
    <Card className={s.section}>
      <div className={s.sectionHead}>
        <Subtitle2 as="h2">{title}</Subtitle2>
        {description && <Caption1 className={s.sub}>{description}</Caption1>}
      </div>
      {children}
    </Card>
  )
}

/** One labelled setting: text on the left, the control on the right. */
export function SettingRow({ id, title, description, children }: {
  id: string
  title: string
  description?: ReactNode
  children: ReactNode
}) {
  const s = useStyles()
  return (
    <div className={s.row}>
      <div className={s.rowText}>
        <Label htmlFor={id} weight="semibold">{title}</Label>
        {description && <Caption1 className={s.sub}>{description}</Caption1>}
      </div>
      <div>{children}</div>
    </div>
  )
}

/** A slim battery bar in the same colours as the tray icon's frame. */
export function BatteryBar({ level, charging, online, low, warn }: {
  level: number | null
  charging: boolean
  online: boolean
  low: number
  warn: number
}) {
  const s = useStyles()
  const colour = !online
    ? tokens.colorNeutralForeground4
    : charging
      ? BATTERY.charging
      : level !== null && level < low
        ? BATTERY.low
        : level !== null && level <= warn
          ? BATTERY.warn
          : tokens.colorNeutralForeground2
  const pct = level === null ? 0 : Math.max(3, Math.min(100, level))
  return (
    <div className={s.track} role="progressbar" aria-valuenow={level ?? undefined} aria-valuemin={0} aria-valuemax={100}>
      <div className={mergeClasses(s.fill, charging && online && s.pulse)}
           style={{ width: `${pct}%`, backgroundColor: colour }} />
    </div>
  )
}
