import { useEffect, useRef, useState } from "react"
import { FolderOpenIcon, InfoIcon } from "lucide-react"
import { toast } from "sonner"

import { PageHeader, SettingRow } from "@/components/setting-row"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Slider } from "@/components/ui/slider"
import { Switch } from "@/components/ui/switch"
import { api, iconUrl, type Kind, type Settings, type State } from "@/lib/api"

type Save = <K extends keyof Settings>(key: K, value: Settings[K]) => Promise<boolean>
type SaveMany = (values: Partial<Settings>) => Promise<boolean>

/** Saves settings as soon as they change (no Save button to forget). */
export function useSave(refresh: () => void): Save & { many: SaveMany } {
  const many: SaveMany = async (values) => {
    try {
      await api.saveSettings(values)
      toast.success("Saved", { id: "saved", duration: 1200 })
      refresh()
      return true
    } catch (e) {
      toast.error((e as Error).message)
      refresh()
      return false
    }
  }
  const one = ((key, value) => many({ [key]: value })) as Save & { many: SaveMany }
  one.many = many
  return one
}

function Section({ title, description, children }: {
  title: string
  description?: string
  children: React.ReactNode
}) {
  return (
    <Card className="gap-2">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="[&>*+*]:border-t">{children}</CardContent>
    </Card>
  )
}

function Toggle({ s, save, k, title, description }: {
  s: Settings
  save: Save
  k: keyof Settings
  title: string
  description?: string
}) {
  return (
    <SettingRow id={k} title={title} description={description}>
      <Switch id={k} checked={Boolean(s[k])} onCheckedChange={(v) => save(k, v as never)} />
    </SettingRow>
  )
}

function Choice({ state, save, k, title, description }: {
  state: State
  save: Save
  k: "poll_seconds" | "alert_at"
  title: string
  description?: string
}) {
  const options = state.choices[k] ?? []
  return (
    <SettingRow id={k} title={title} description={description}>
      <Select value={String(state.settings[k])} onValueChange={(v) => save(k, Number(v))}>
        <SelectTrigger id={k} className="w-40"><SelectValue /></SelectTrigger>
        <SelectContent>
          {options.map(([value, label]) => <SelectItem key={value} value={String(value)}>{label}</SelectItem>)}
        </SelectContent>
      </Select>
    </SettingRow>
  )
}

// --- pages ----------------------------------------------------------------------

export function GeneralPage({ state, save }: { state: State; save: Save }) {
  const s = state.settings
  return (
    <>
      <PageHeader title="General" description="How often the app looks, and when it starts." />
      <div className="grid gap-4">
        <Section title="Checking">
          <Choice state={state} save={save} k="poll_seconds" title="Check batteries every"
                  description="Mute and power changes still show up instantly." />
        </Section>
        <Section title="Startup and data">
          <Toggle s={s} save={save} k="autostart" title="Start with Windows"
                  description="Runs quietly in the tray when you sign in." />
          <Toggle s={s} save={save} k="history" title="Keep a battery history"
                  description="Writes history.csv in the data folder, for charts or curiosity." />
        </Section>
      </div>
    </>
  )
}

const PREVIEW: [number | null, boolean][] = [[8, false], [25, false], [64, false], [95, false], [40, true]]

export function AlertsPage({ state, save }: { state: State; save: Save & { many: SaveMany } }) {
  const s = state.settings
  const [range, setRange] = useState<[number, number]>([s.low, s.warn])
  const [kind, setKind] = useState<Kind>("mouse")
  const pending = useRef<number | undefined>(undefined)
  const editing = useRef(false)
  // Follow the saved values, except while the user is still moving the slider.
  useEffect(() => {
    if (!editing.current) setRange([s.low, s.warn])
  }, [s.low, s.warn])
  useEffect(() => () => window.clearTimeout(pending.current), [])

  function commit([low, warn]: number[]) {
    editing.current = true
    window.clearTimeout(pending.current)
    // Arrow keys commit on every press: save once, shortly after the last one.
    pending.current = window.setTimeout(async () => {
      if (warn > low && (low !== s.low || warn !== s.warn)) await save.many({ low, warn })
      editing.current = false
    }, 400)
  }

  return (
    <>
      <PageHeader title="Alerts & colours" description="When the frame turns yellow or red, and when to notify you." />
      <div className="grid gap-4">
        <Section title="Frame colours"
                 description="Green while charging, red below the first limit, yellow up to the second.">
          <div className="grid gap-5 py-4">
            <Slider aria-label="Colour limits" min={5} max={80} step={1} value={range} minStepsBetweenThumbs={2}
                    onValueChange={(v) => setRange([v[0], v[1]])} onValueCommit={commit} />
            <div className="text-muted-foreground flex justify-between text-[13px]">
              <span><span className="text-battery-low font-medium">Red</span> below {range[0]}%</span>
              <span><span className="text-battery-warn font-medium">Yellow</span> {range[0]}–{range[1]}%</span>
              <span>Normal above {range[1]}%</span>
            </div>
            <div className="bg-muted/50 flex items-center justify-between rounded-xl p-4" data-testid="icon-preview">
              {PREVIEW.map(([level, charging]) => (
                <figure key={`${level}${charging}`} className="grid justify-items-center gap-1.5">
                  <img alt="" width={40} height={40} className="size-10"
                       src={iconUrl({ kind, level, charging, low: range[0], warn: range[1], size: 40,
                                      number: s.show_number })} />
                  <figcaption className="text-muted-foreground text-xs">{charging ? "charging" : `${level}%`}</figcaption>
                </figure>
              ))}
              <Select value={kind} onValueChange={(v) => setKind(v as Kind)}>
                <SelectTrigger className="w-32" aria-label="Preview device"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(["mouse", "headset", "keyboard", "gamepad"] as Kind[]).map((k) => (
                    <SelectItem key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </Section>
        <Section title="Notifications">
          <Choice state={state} save={save} k="alert_at" title="Low battery notification"
                  description="Once per discharge; it re-arms after the device has been charged." />
          <Toggle s={s} save={save} k="notify_full" title="Fully charged"
                  description="A reminder to unplug, which is kinder to the battery." />
        </Section>
      </div>
    </>
  )
}

export function AppearancePage({ state, save }: { state: State; save: Save }) {
  const s = state.settings
  return (
    <>
      <PageHeader title="Appearance" description="What the tray icons show." />
      <div className="grid gap-4">
        <Section title="Icon">
          <SettingRow id="show_number" title="Show the percentage instead of the picture"
                      description="The device picture fills up with the level; this shows the number instead.">
            <div className="flex items-center gap-3">
              <img alt="" width={32} height={32} className="size-8"
                   src={iconUrl({ kind: "headset", level: 64, number: s.show_number, size: 32 })} />
              <Switch id="show_number" checked={s.show_number} onCheckedChange={(v) => save("show_number", v)} />
            </div>
          </SettingRow>
        </Section>
        <Section title="Microphone">
          <Toggle s={s} save={save} k="flash_on_mute" title="Blink the headset icon while its mic is muted" />
          <Toggle s={s} save={save} k="windows_mute" title="Count a mic muted in Windows as muted"
                  description="As well as the headset's own mute button. Left-click a headset icon to toggle it." />
        </Section>
      </div>
    </>
  )
}

export function SourcesPage({ state, save }: { state: State; save: Save }) {
  const s = state.settings
  const [hc, setHc] = useState(s.headsetcontrol)
  useEffect(() => setHc(s.headsetcontrol), [s.headsetcontrol])
  return (
    <>
      <PageHeader title="Sources" description="Where battery levels come from." />
      <div className="grid gap-4">
        <Section title="Built in" description="Logitech, Razer, HyperX, SteelSeries and Corsair are always on.">
          <Toggle s={s} save={save} k="bluetooth" title="Bluetooth devices"
                  description="Anything Windows shows a battery for: keyboards like the Keychron K8 Pro, earbuds, headphones." />
          <Toggle s={s} save={save} k="xinput" title="Xbox-compatible controllers"
                  description="Xbox pads, most 2.4 GHz pads, and headsets plugged into a controller." />
        </Section>
        <Section title="HeadsetControl" description="Optional: adds 100+ more headsets.">
          <div className="grid gap-2 py-3.5">
            <label htmlFor="hc" className="text-sm font-medium">Path to headsetcontrol.exe</label>
            <div className="flex gap-2">
              <Input id="hc" value={hc} placeholder="Empty = find it on the PATH"
                     onChange={(e) => setHc(e.target.value)}
                     onBlur={() => hc !== s.headsetcontrol && save("headsetcontrol", hc)}
                     onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
            </div>
            <p className="text-muted-foreground text-[13px]">
              Get it from github.com/Sapd/HeadsetControl. Headsets this app already reads are skipped.
            </p>
          </div>
        </Section>
        <Alert>
          <InfoIcon />
          <AlertTitle>Device not listed?</AlertTitle>
          <AlertDescription>
            Use <b>Learn a device</b> to teach the app an unsupported USB dongle in about a minute.
            Extra recipes live in <code className="font-mono text-xs">recipes.json</code> in the data folder.
          </AlertDescription>
        </Alert>
      </div>
    </>
  )
}

export function AboutPage({ state }: { state: State }) {
  return (
    <>
      <PageHeader title="About" />
      <Card>
        <CardContent className="grid gap-4 text-sm">
          <div className="flex items-center gap-4">
            <img src={iconUrl({ kind: "headset", level: 70, charging: true, size: 64 })} alt="" className="size-14" />
            <div>
              <div className="text-lg font-semibold">Peripherals Battery</div>
              <div className="text-muted-foreground">Version {state.version}</div>
            </div>
          </div>
          <Separator />
          <p className="text-muted-foreground leading-relaxed">
            Battery levels of your wireless mouse, headset, keyboard and controllers in the Windows tray - no
            vendor software needed. Open source under the MIT licence. Protocol knowledge thanks to Solaar,
            HeadsetControl, HyperHeadset and OpenRazer; the per-device tray idea was inspired by HaloBattery.
          </p>
          <p className="text-muted-foreground flex items-center gap-2">
            <FolderOpenIcon className="size-4" /> Settings, history and recipes live in %APPDATA%\PeripheralsBattery.
          </p>
        </CardContent>
      </Card>
    </>
  )
}
