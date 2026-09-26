import { useState } from "react"
import {
  BatteryChargingIcon,
  EyeIcon,
  EyeOffIcon,
  MicOffIcon,
  MoreHorizontalIcon,
  PencilIcon,
  PlugZapIcon,
  PowerOffIcon,
  SparklesIcon,
  Trash2Icon,
} from "lucide-react"
import { toast } from "sonner"

import { BatteryBar } from "@/components/battery"
import { PageHeader } from "@/components/setting-row"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { api, withToken, type Device, type State } from "@/lib/api"

const KIND_LABEL: Record<string, string> = {
  mouse: "Mouse",
  headset: "Headset",
  keyboard: "Keyboard",
  gamepad: "Controller",
  device: "Device",
}

export function DevicesPage({ state, refresh, go }: {
  state: State | null
  refresh: () => void
  go: (page: string) => void
}) {
  const [renaming, setRenaming] = useState<Device | null>(null)
  const [forgetting, setForgetting] = useState<Device | null>(null)

  async function act(d: Device, action: "hide" | "show" | "forget", done: string) {
    try {
      await api.device(d.key, action)
      toast.success(done)
      refresh()
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const devices = state?.devices ?? []
  return (
    <>
      <PageHeader
        title="Devices"
        description="Everything the app has found. Each one gets its own tray icon."
        actions={<Button variant="outline" size="sm" onClick={() => go("learn")}>
          <SparklesIcon /> Learn a new device
        </Button>}
      />

      {state === null ? (
        <div className="grid gap-4 sm:grid-cols-2">
          {[0, 1].map((i) => <Skeleton key={i} className="h-36 rounded-xl" />)}
        </div>
      ) : devices.length === 0 ? (
        <Empty className="border border-dashed">
          <EmptyHeader>
            <EmptyMedia variant="icon"><PlugZapIcon /></EmptyMedia>
            <EmptyTitle>No devices yet</EmptyTitle>
            <EmptyDescription>
              Switch your wireless gear on and plug in its receiver. Bluetooth devices appear as soon as
              Windows shows their battery.
            </EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button variant="outline" onClick={() => go("learn")}><SparklesIcon /> Learn a new device</Button>
          </EmptyContent>
        </Empty>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2" data-testid="device-grid">
          {devices.map((d) => (
            <DeviceCard key={d.key} d={d} low={state.settings.low} warn={state.settings.warn}
                        onRename={() => setRenaming(d)}
                        onToggle={() => act(d, d.hidden ? "show" : "hide",
                                            d.hidden ? `${d.name} is back in the tray` : `${d.name} hidden from the tray`)}
                        onForget={() => setForgetting(d)} />
          ))}
        </div>
      )}

      <RenameDialog device={renaming} onClose={() => setRenaming(null)} onDone={refresh} />

      <AlertDialog open={forgetting !== null} onOpenChange={(o) => !o && setForgetting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Forget {forgetting?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              Its grey icon and remembered battery level are removed. It comes back automatically the next
              time it is switched on.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => forgetting && act(forgetting, "forget", `${forgetting.name} forgotten`)}>
              Forget
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

function DeviceCard({ d, low, warn, onRename, onToggle, onForget }: {
  d: Device
  low: number
  warn: number
  onRename: () => void
  onToggle: () => void
  onForget: () => void
}) {
  return (
    <Card className={d.hidden ? "opacity-60" : ""} data-testid="device-card">
      <CardContent className="flex flex-col gap-4">
        <div className="flex items-start gap-4">
          <div className="bg-muted/60 dark:bg-muted/40 grid size-14 shrink-0 place-items-center rounded-xl">
            <img src={withToken(`${d.icon}&size=48`)} alt="" width={40} height={40} className="size-10" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3 className="line-clamp-2 font-medium leading-snug" title={d.name}>{d.name}</h3>
            </div>
            <p className="text-muted-foreground truncate text-[13px]">
              {KIND_LABEL[d.kind] ?? "Device"}
              {d.name !== d.original && <> · {d.original}</>}
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {!d.online && <Badge variant="outline"><PowerOffIcon /> {d.note || "Off"}</Badge>}
              {d.online && d.charging && (
                <Badge className="bg-battery-charging/15 text-battery-charging border-battery-charging/30">
                  <BatteryChargingIcon /> Charging
                </Badge>
              )}
              {d.online && d.muted && (
                <Badge variant="destructive"><MicOffIcon /> Mic muted</Badge>
              )}
              {d.hidden && <Badge variant="secondary"><EyeOffIcon /> Hidden</Badge>}
            </div>
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" className="size-8" aria-label={`Actions for ${d.name}`}>
                <MoreHorizontalIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={onRename}><PencilIcon /> Rename</DropdownMenuItem>
              <DropdownMenuItem onSelect={onToggle}>
                {d.hidden ? <><EyeIcon /> Show in tray</> : <><EyeOffIcon /> Hide from tray</>}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" disabled={d.online} onSelect={onForget}>
                <Trash2Icon /> Forget
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div className="grid gap-2">
          <div className="flex items-baseline justify-between">
            <span className="text-3xl font-semibold tabular-nums tracking-tight">
              {d.level === null ? "–" : `${d.note === "approximate" ? "~" : ""}${d.level}%`}
            </span>
            <span className="text-muted-foreground text-[13px]">
              {d.online ? d.estimate ?? (d.charging ? "Charging" : "") : "Last known level"}
            </span>
          </div>
          <BatteryBar level={d.level} charging={d.charging} online={d.online} low={low} warn={warn} />
        </div>
      </CardContent>
    </Card>
  )
}

function RenameDialog({ device, onClose, onDone }: {
  device: Device | null
  onClose: () => void
  onDone: () => void
}) {
  const [name, setName] = useState("")
  const [prev, setPrev] = useState<Device | null>(null)
  if (device !== prev) {
    setPrev(device)
    setName(device?.name ?? "")
  }

  async function save(e: React.FormEvent) {
    e.preventDefault()
    if (!device) return
    try {
      await api.device(device.key, "rename", name)
      toast.success(name.trim() ? `Renamed to ${name.trim()}` : `Back to ${device.original}`)
      onDone()
      onClose()
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <Dialog open={device !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={save} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Rename device</DialogTitle>
            <DialogDescription>
              Shown in the tooltip and notifications. Leave empty to use “{device?.original}”.
            </DialogDescription>
          </DialogHeader>
          <Input autoFocus value={name} maxLength={60} onChange={(e) => setName(e.target.value)}
                 placeholder={device?.original} aria-label="Device name" />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit">Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
