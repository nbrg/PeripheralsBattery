import { cn } from "@/lib/utils"

/** A slim battery bar in the same colours as the tray icon's frame. */
export function BatteryBar({ level, charging, online, low, warn, className }: {
  level: number | null
  charging: boolean
  online: boolean
  low: number
  warn: number
  className?: string
}) {
  const pct = Math.max(0, Math.min(100, level ?? 0))
  const colour = !online
    ? "bg-muted-foreground/40"
    : charging
      ? "bg-battery-charging"
      : level !== null && level < low
        ? "bg-battery-low"
        : level !== null && level <= warn
          ? "bg-battery-warn"
          : "bg-battery-ok"
  return (
    <div className={cn("bg-muted h-2 w-full overflow-hidden rounded-full", className)}
         role="progressbar" aria-valuenow={level ?? undefined} aria-valuemin={0} aria-valuemax={100}>
      <div className={cn("h-full rounded-full transition-[width] duration-500", colour,
                         charging && online && "animate-pulse")}
           style={{ width: `${level === null ? 0 : Math.max(pct, 3)}%` }} />
    </div>
  )
}
