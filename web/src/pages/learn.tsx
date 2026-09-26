import { useEffect, useRef, useState } from "react"
import {
  BatteryMediumIcon,
  CheckCircle2Icon,
  CheckIcon,
  CopyIcon,
  MicOffIcon,
  PlugIcon,
  RefreshCwIcon,
  SaveIcon,
  SearchXIcon,
  UsbIcon,
} from "lucide-react"
import { toast } from "sonner"

import { PageHeader } from "@/components/setting-row"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { api, type Analysis, type Kind, type LearnDevice } from "@/lib/api"
import { cn } from "@/lib/utils"

const STEPS = ["Device", "Battery", "Mute", "Charging", "Save"] as const

/** Seconds per countdown tick; the end-to-end tests speed this up with ?fast=1. */
const TICK_MS = new URLSearchParams(window.location.search).get("fast") ? 20 : 1000

type Phase = { phase: string; text: string; seconds: number }

/** Runs timed phases, telling the app which one is active so reports get labelled. */
function usePhases() {
  const [current, setCurrent] = useState<{ text: string; left: number } | null>(null)
  const [progress, setProgress] = useState(0)
  const cancelled = useRef(false)
  useEffect(() => () => { cancelled.current = true }, [])

  async function run(phases: Phase[]) {
    const total = phases.reduce((n, p) => n + p.seconds, 0)
    let elapsed = 0
    for (const p of phases) {
      await api.learn.phase(p.phase)
      for (let left = p.seconds; left > 0; left--) {
        if (cancelled.current) return
        setCurrent({ text: p.text, left })
        setProgress((100 * elapsed) / total)
        await new Promise((r) => setTimeout(r, TICK_MS))
        elapsed++
      }
    }
    await api.learn.phase("idle")
    setProgress(100)
    setCurrent(null)
  }
  return { current, progress, run }
}

export function LearnPage() {
  const [step, setStep] = useState(0)
  const [device, setDevice] = useState<LearnDevice | null>(null)
  const [results, setResults] = useState<Partial<Record<"level" | "muted" | "charging", Analysis>>>({})
  useEffect(() => () => { api.learn.close().catch(() => {}) }, [])

  function restart() {
    api.learn.close().catch(() => {})
    setDevice(null)
    setResults({})
    setStep(0)
  }
  const found = (Object.values(results) as Analysis[]).some((r) => r.found)

  return (
    <>
      <PageHeader title="Learn a new device"
                  description="Teach the app a USB dongle it doesn't know yet. Takes about a minute." />
      <ol className="mb-6 flex items-center gap-2" aria-label="Progress">
        {STEPS.map((label, i) => (
          <li key={label} className="flex flex-1 items-center gap-2">
            <span className={cn(
              "grid size-7 shrink-0 place-items-center rounded-full border text-xs font-medium transition-colors",
              i < step && "bg-primary text-primary-foreground border-primary",
              i === step && "border-primary text-foreground ring-primary/20 ring-4",
              i > step && "text-muted-foreground")}>
              {i < step ? <CheckIcon className="size-3.5" /> : i + 1}
            </span>
            <span className={cn("text-sm", i === step ? "font-medium" : "text-muted-foreground")}>{label}</span>
            {i < STEPS.length - 1 && <span className="bg-border h-px flex-1" />}
          </li>
        ))}
      </ol>

      {step === 0 && <PickStep onPicked={(d) => { setDevice(d); setStep(1) }} />}
      {step === 1 && <BatteryStep onDone={(r) => { if (r) setResults((x) => ({ ...x, level: r })); setStep(2) }} />}
      {step === 2 && (
        <ToggleStep what="muted" icon={<MicOffIcon />} title="Microphone mute"
                    description="For headsets: follow the prompts to unmute, mute and unmute the mic."
                    phases={[
                      { phase: "unmuted", text: "Make sure the mic is unmuted", seconds: 5 },
                      { phase: "muted", text: "Now mute the mic", seconds: 6 },
                      { phase: "unmuted2", text: "Now unmute it again", seconds: 6 },
                    ]}
                    onDone={(r) => { if (r) setResults((x) => ({ ...x, muted: r })); setStep(3) }} />
      )}
      {step === 3 && (
        <ToggleStep what="charging" icon={<PlugIcon />} title="Charging"
                    description="Unplug and plug in the charging cable when asked. Skip it if the device charges over its data cable."
                    phases={[
                      { phase: "unplugged", text: "Unplug the charging cable", seconds: 8 },
                      { phase: "plugged", text: "Now plug it in", seconds: 10 },
                    ]}
                    onDone={(r) => { if (r) setResults((x) => ({ ...x, charging: r })); setStep(4) }} />
      )}
      {step === 4 && (found && device
        ? <SaveStep device={device} results={results} onRestart={restart} />
        : <NothingFound onRestart={restart} />)}
    </>
  )
}

function PickStep({ onPicked }: { onPicked: (d: LearnDevice) => void }) {
  const [all, setAll] = useState(false)
  const [devices, setDevices] = useState<LearnDevice[] | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [opening, setOpening] = useState(false)

  async function load() {
    setDevices(null)
    try {
      setDevices(await api.learn.devices(all))
    } catch (e) {
      toast.error((e as Error).message)
      setDevices([])
    }
  }
  useEffect(() => { load() }, [all])  // eslint-disable-line react-hooks/exhaustive-deps

  async function next() {
    const d = devices?.find((x) => x.id === selected)
    if (!d) return
    setOpening(true)
    try {
      const res = await api.learn.open(d.id)
      if (!res.ok) throw new Error("Couldn't open this device. Close its vendor app and try again.")
      onPicked(d)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setOpening(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Which device?</CardTitle>
        <CardDescription>Plug in its dongle and switch it on. Devices the app already reads are hidden.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="grid max-h-72 gap-2 overflow-y-auto pr-1" role="listbox" aria-label="USB devices">
          {devices === null && <div className="text-muted-foreground flex items-center gap-2 p-4 text-sm"><Spinner /> Looking…</div>}
          {devices?.length === 0 && (
            <p className="text-muted-foreground p-4 text-sm">No unsupported USB devices found. Is the dongle plugged in?</p>
          )}
          {devices?.map((d) => (
            <button key={d.id} role="option" aria-selected={selected === d.id} onClick={() => setSelected(d.id)}
                    className={cn("hover:bg-accent flex items-center gap-3 rounded-lg border p-3 text-left transition-colors",
                                  selected === d.id && "border-primary bg-accent ring-primary/15 ring-2")}>
              <UsbIcon className="text-muted-foreground size-5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">{d.name}</div>
                <div className="text-muted-foreground text-xs">{d.maker || "Unknown maker"} · {d.vid}:{d.pid}</div>
              </div>
              {d.supported && <Badge variant="secondary">Supported</Badge>}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <Switch id="all" checked={all} onCheckedChange={setAll} />
          <Label htmlFor="all" className="text-muted-foreground font-normal">Show devices that are already supported</Label>
        </div>
      </CardContent>
      <CardFooter className="justify-between">
        <Button variant="ghost" onClick={load}><RefreshCwIcon /> Refresh</Button>
        <Button onClick={next} disabled={selected === null || opening}>{opening && <Spinner />} Next</Button>
      </CardFooter>
    </Card>
  )
}

function Countdown({ current, progress }: { current: { text: string; left: number } | null; progress: number }) {
  return (
    <div className="bg-muted/50 grid gap-3 rounded-xl p-4">
      <div className="flex items-baseline justify-between">
        <span className="text-lg font-medium">{current?.text ?? "Done"}</span>
        {current && <span className="text-muted-foreground tabular-nums">{current.left}s</span>}
      </div>
      <Progress value={progress} />
    </div>
  )
}

function Result({ r, what }: { r: Analysis; what: string }) {
  return r.found ? (
    <Alert className="border-battery-charging/40">
      <CheckCircle2Icon className="text-battery-charging" />
      <AlertTitle>Found the {what}</AlertTitle>
      <AlertDescription>{r.description} · {r.reports} reports heard</AlertDescription>
    </Alert>
  ) : (
    <Alert>
      <SearchXIcon />
      <AlertTitle>No {what} signal found</AlertTitle>
      <AlertDescription>
        Heard {r.reports} reports, none matched. You can try again or skip this step.
      </AlertDescription>
    </Alert>
  )
}

function BatteryStep({ onDone }: { onDone: (r: Analysis | null) => void }) {
  const [level, setLevel] = useState("")
  const [probe, setProbe] = useState(true)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<Analysis | null>(null)
  const phases = usePhases()

  async function listen() {
    setRunning(true)
    setResult(null)
    try {
      await api.learn.phase("battery")
      if (probe) await api.learn.queries()
      await phases.run([{ phase: "battery", text: "Listening — leave the device alone", seconds: 15 }])
      const pct = Number.parseInt(level, 10)
      setResult(await api.learn.analyze("level", Number.isNaN(pct) ? undefined : pct))
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><BatteryMediumIcon className="size-5" /> Battery level</CardTitle>
        <CardDescription>
          Enter the level the device reports right now (from its app, a voice prompt or its LEDs), then listen.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid max-w-48 gap-2">
          <Label htmlFor="level">Battery now</Label>
          <div className="relative">
            <Input id="level" inputMode="numeric" value={level} onChange={(e) => setLevel(e.target.value)}
                   placeholder="e.g. 57" className="pr-8" />
            <span className="text-muted-foreground absolute top-1/2 right-3 -translate-y-1/2 text-sm">%</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Switch id="probe" checked={probe} onCheckedChange={setProbe} />
          <Label htmlFor="probe" className="font-normal">Also ask the battery questions other brands understand (read-only)</Label>
        </div>
        {(running || result) && <Countdown current={phases.current} progress={phases.progress} />}
        {result && <Result r={result} what="battery level" />}
      </CardContent>
      <CardFooter className="justify-between">
        <Button variant="ghost" onClick={() => onDone(null)} disabled={running}>Skip</Button>
        {result?.found
          ? <Button onClick={() => onDone(result)}>Next</Button>
          : <Button onClick={listen} disabled={running}>{running && <Spinner />} {result ? "Listen again" : "Listen"}</Button>}
      </CardFooter>
    </Card>
  )
}

function ToggleStep({ what, icon, title, description, phases: plan, onDone }: {
  what: "muted" | "charging"
  icon: React.ReactNode
  title: string
  description: string
  phases: Phase[]
  onDone: (r: Analysis | null) => void
}) {
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<Analysis | null>(null)
  const phases = usePhases()

  async function start() {
    setRunning(true)
    setResult(null)
    try {
      await phases.run(plan)
      setResult(await api.learn.analyze(what))
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 [&_svg]:size-5">{icon} {title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {(running || result) && <Countdown current={phases.current} progress={phases.progress} />}
        {result && <Result r={result} what={what === "muted" ? "mute signal" : "charging signal"} />}
      </CardContent>
      <CardFooter className="justify-between">
        <Button variant="ghost" onClick={() => onDone(null)} disabled={running}>Skip</Button>
        {result?.found
          ? <Button onClick={() => onDone(result)}>Next</Button>
          : <Button onClick={start} disabled={running}>{running && <Spinner />} {result ? "Try again" : "Start"}</Button>}
      </CardFooter>
    </Card>
  )
}

function SaveStep({ device, results, onRestart }: {
  device: LearnDevice
  results: Partial<Record<string, Analysis>>
  onRestart: () => void
}) {
  const [name, setName] = useState(device.name)
  const [kind, setKind] = useState<Kind>(results.muted?.found ? "headset" : "device")
  const [recipe, setRecipe] = useState("")
  const [saved, setSaved] = useState<string | null>(null)

  useEffect(() => {
    const t = setTimeout(() => {
      api.learn.recipe(name, kind).then((r) => setRecipe(JSON.stringify(r.recipe, null, 2))).catch(() => {})
    }, 150)
    return () => clearTimeout(t)
  }, [name, kind])

  async function save() {
    try {
      const r = await api.learn.save(name, kind)
      setSaved(r.path)
      toast.success(`${name} learned — its icon appears within a few seconds`)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  async function copy() {
    await navigator.clipboard.writeText(recipe)
    toast.success("Recipe copied — share it with a friend who has the same device")
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Name and save</CardTitle>
        <CardDescription>This becomes a recipe. Share it and anyone with the same device gets support.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap gap-2">
          {(["level", "muted", "charging"] as const).map((k) => (
            <Badge key={k} variant={results[k]?.found ? "default" : "outline"}>
              {results[k]?.found ? <CheckIcon /> : null}
              {{ level: "Battery", muted: "Mic mute", charging: "Charging" }[k]}
            </Badge>
          ))}
        </div>
        <div className="grid gap-4 sm:grid-cols-[1fr_12rem]">
          <div className="grid gap-2">
            <Label htmlFor="name">Name</Label>
            <Input id="name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="kind">Type</Label>
            <Select value={kind} onValueChange={(v) => setKind(v as Kind)}>
              <SelectTrigger id="kind" className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {(["headset", "mouse", "keyboard", "gamepad", "device"] as Kind[]).map((k) => (
                  <SelectItem key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <pre className="bg-muted/60 max-h-56 overflow-auto rounded-lg p-3 font-mono text-xs leading-relaxed"
             data-testid="recipe">{recipe}</pre>
        {saved && (
          <Alert className="border-battery-charging/40">
            <CheckCircle2Icon className="text-battery-charging" />
            <AlertTitle>Saved</AlertTitle>
            <AlertDescription className="break-all">{saved}</AlertDescription>
          </Alert>
        )}
      </CardContent>
      <CardFooter className="justify-between">
        <Button variant="ghost" onClick={onRestart}>Learn another</Button>
        <div className="flex gap-2">
          <Button variant="outline" onClick={copy} disabled={!recipe}><CopyIcon /> Copy</Button>
          <Button onClick={save} disabled={!recipe || saved !== null}><SaveIcon /> Save</Button>
        </div>
      </CardFooter>
    </Card>
  )
}

function NothingFound({ onRestart }: { onRestart: () => void }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><SearchXIcon className="size-5" /> Nothing recognised</CardTitle>
        <CardDescription>
          None of the steps found a usable signal. This device probably needs commands only its vendor app knows.
        </CardDescription>
      </CardHeader>
      <CardContent className="text-muted-foreground text-sm">
        Run <code className="bg-muted rounded px-1.5 py-0.5 font-mono text-xs">PeripheralsBattery.exe --probe</code> and
        share the report in an issue on GitHub to get it supported.
      </CardContent>
      <CardFooter><Button variant="outline" onClick={onRestart}>Start over</Button></CardFooter>
    </Card>
  )
}
