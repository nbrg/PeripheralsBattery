import { useEffect, useRef, useState, type ReactNode } from "react"
import {
  Badge,
  Body1,
  Button,
  Caption1,
  Card,
  Dropdown,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Option,
  ProgressBar,
  Spinner,
  Subtitle1,
  Switch,
  Text,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components"
import {
  ArrowSyncRegular,
  Battery5Regular,
  CheckmarkRegular,
  CopyRegular,
  MicOffRegular,
  PlugConnectedRegular,
  SaveRegular,
  SearchRegular,
  UsbStickRegular,
} from "@fluentui/react-icons"

import { PageHeader } from "@/components/common"
import { useNotify } from "@/components/notify"
import { api, type Analysis, type Kind, type LearnDevice } from "@/lib/api"

const STEPS = ["Device", "Battery", "Mute", "Charging", "Save"] as const

/** Seconds per countdown tick; the end-to-end tests speed this up with ?fast=1. */
const TICK_MS = new URLSearchParams(window.location.search).get("fast") ? 20 : 1000

type Phase = { phase: string; text: string; seconds: number }

const useStyles = makeStyles({
  steps: { display: "flex", alignItems: "center", gap: "8px", margin: "0 0 24px", padding: 0, listStyle: "none" },
  step: { display: "flex", flex: 1, alignItems: "center", gap: "8px" },
  dot: {
    display: "grid",
    placeItems: "center",
    width: "28px",
    height: "28px",
    flexShrink: 0,
    borderRadius: tokens.borderRadiusCircular,
    border: `1px solid ${tokens.colorNeutralStroke1}`,
    fontSize: tokens.fontSizeBase200,
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground3,
  },
  dotDone: {
    backgroundColor: tokens.colorBrandBackground,
    color: tokens.colorNeutralForegroundOnBrand,
    border: `1px solid ${tokens.colorBrandBackground}`,
  },
  dotNow: {
    border: `2px solid ${tokens.colorBrandStroke1}`,
    color: tokens.colorNeutralForeground1,
    boxShadow: `0 0 0 4px ${tokens.colorBrandBackground2}`,
  },
  line: { flex: 1, height: "1px", backgroundColor: tokens.colorNeutralStroke2 },
  muted: { color: tokens.colorNeutralForeground3 },
  card: { padding: "24px", gap: "20px" },
  cardHead: { display: "grid", gap: "4px" },
  title: { display: "flex", alignItems: "center", gap: "8px", fontSize: "20px" },
  body: { display: "grid", gap: "16px" },
  footer: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" },
  list: { display: "grid", gap: "8px", maxHeight: "300px", overflowY: "auto", paddingRight: "4px" },
  option: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    width: "100%",
    padding: "12px",
    textAlign: "left",
    cursor: "pointer",
    color: tokens.colorNeutralForeground1,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusLarge,
    transitionProperty: "background-color, border-color, box-shadow",
    transitionDuration: tokens.durationFaster,
    ":hover": { backgroundColor: tokens.colorNeutralBackground1Hover },
  },
  optionSelected: {
    border: `1px solid ${tokens.colorBrandStroke1}`,
    backgroundColor: tokens.colorBrandBackground2,
    boxShadow: `0 0 0 1px ${tokens.colorBrandStroke1}`,
    ":hover": { backgroundColor: tokens.colorBrandBackground2Hover },
  },
  optionText: { display: "grid", minWidth: 0, flex: 1 },
  usb: { fontSize: "20px", color: tokens.colorNeutralForeground3 },
  countdown: {
    display: "grid",
    gap: "12px",
    padding: "16px",
    borderRadius: tokens.borderRadiusXLarge,
    backgroundColor: tokens.colorNeutralBackground3,
  },
  countdownRow: { display: "flex", alignItems: "baseline", justifyContent: "space-between" },
  pct: { position: "relative", maxWidth: "200px" },
  inline: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" },
  saveGrid: { display: "grid", gridTemplateColumns: "1fr 200px", gap: "16px" },
  pre: {
    margin: 0,
    maxHeight: "220px",
    overflow: "auto",
    padding: "12px 14px",
    borderRadius: tokens.borderRadiusLarge,
    backgroundColor: tokens.colorNeutralBackground3,
    fontFamily: tokens.fontFamilyMonospace,
    fontSize: tokens.fontSizeBase200,
    lineHeight: "1.6",
  },
  code: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200 },
})

function StepCard({ icon, title, description, children, footer }: {
  icon?: ReactNode
  title: string
  description: string
  children?: ReactNode
  footer: ReactNode
}) {
  const s = useStyles()
  return (
    <Card className={s.card}>
      <div className={s.cardHead}>
        <Subtitle1 as="h2" className={s.title} style={{ margin: 0 }}>{icon}{title}</Subtitle1>
        <Body1 className={s.muted}>{description}</Body1>
      </div>
      {children && <div className={s.body}>{children}</div>}
      <div className={s.footer}>{footer}</div>
    </Card>
  )
}

/** Runs timed phases, telling the app which one is active so reports get labelled. */
function usePhases() {
  const [current, setCurrent] = useState<{ text: string; left: number } | null>(null)
  const [progress, setProgress] = useState(0)
  const cancelled = useRef(false)
  useEffect(() => {
    cancelled.current = false
    return () => { cancelled.current = true }
  }, [])

  async function run(phases: Phase[]) {
    const total = phases.reduce((n, p) => n + p.seconds, 0)
    let elapsed = 0
    for (const p of phases) {
      await api.learn.phase(p.phase)
      for (let left = p.seconds; left > 0; left--) {
        if (cancelled.current) return
        setCurrent({ text: p.text, left })
        setProgress(elapsed / total)
        await new Promise((r) => setTimeout(r, TICK_MS))
        elapsed++
      }
    }
    await api.learn.phase("idle")
    setProgress(1)
    setCurrent(null)
  }
  return { current, progress, run }
}

export function LearnPage() {
  const s = useStyles()
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
      <ol className={s.steps} aria-label="Progress">
        {STEPS.map((label, i) => (
          <li key={label} className={s.step}>
            <span className={mergeClasses(s.dot, i < step && s.dotDone, i === step && s.dotNow)}>
              {i < step ? <CheckmarkRegular /> : i + 1}
            </span>
            <Text weight={i === step ? "semibold" : "regular"} className={i === step ? undefined : s.muted}>{label}</Text>
            {i < STEPS.length - 1 && <span className={s.line} />}
          </li>
        ))}
      </ol>

      {step === 0 && <PickStep onPicked={(d) => { setDevice(d); setStep(1) }} />}
      {step === 1 && <BatteryStep onDone={(r) => { if (r) setResults((x) => ({ ...x, level: r })); setStep(2) }} />}
      {step === 2 && (
        <ToggleStep what="muted" icon={<MicOffRegular />} title="Microphone mute"
                    description="For headsets: follow the prompts to unmute, mute and unmute the mic."
                    phases={[
                      { phase: "unmuted", text: "Make sure the mic is unmuted", seconds: 5 },
                      { phase: "muted", text: "Now mute the mic", seconds: 6 },
                      { phase: "unmuted2", text: "Now unmute it again", seconds: 6 },
                    ]}
                    onDone={(r) => { if (r) setResults((x) => ({ ...x, muted: r })); setStep(3) }} />
      )}
      {step === 3 && (
        <ToggleStep what="charging" icon={<PlugConnectedRegular />} title="Charging"
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
  const s = useStyles()
  const notify = useNotify()
  const [all, setAll] = useState(false)
  const [devices, setDevices] = useState<LearnDevice[] | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [opening, setOpening] = useState(false)

  async function load(showAll = all) {
    setDevices(null)
    try {
      setDevices(await api.learn.devices(showAll))
    } catch (e) {
      notify((e as Error).message, "error")
      setDevices([])
    }
  }
  useEffect(() => { load(all) }, [all])  // eslint-disable-line react-hooks/exhaustive-deps

  async function next() {
    const d = devices?.find((x) => x.id === selected)
    if (!d) return
    setOpening(true)
    try {
      const res = await api.learn.open(d.id)
      if (!res.ok) throw new Error("Couldn't open this device. Close its vendor app and try again.")
      onPicked(d)
    } catch (e) {
      notify((e as Error).message, "error")
    } finally {
      setOpening(false)
    }
  }

  return (
    <StepCard title="Which device?"
              description="Plug in its dongle and switch it on. Devices the app already reads are hidden."
              footer={<>
                <Button appearance="subtle" icon={<ArrowSyncRegular />} onClick={() => load()}>Refresh</Button>
                <Button appearance="primary" disabled={selected === null || opening} onClick={next}
                        icon={opening ? <Spinner size="tiny" /> : undefined}>Next</Button>
              </>}>
      <div className={s.list} role="listbox" aria-label="USB devices">
        {devices === null && <Spinner size="small" label="Looking for devices…" labelPosition="after" />}
        {devices?.length === 0 && (
          <Body1 className={s.muted}>No unsupported USB devices found. Is the dongle plugged in?</Body1>
        )}
        {devices?.map((d) => (
          <button key={d.id} role="option" aria-selected={selected === d.id} onClick={() => setSelected(d.id)}
                  className={mergeClasses(s.option, selected === d.id && s.optionSelected)}>
            <UsbStickRegular className={s.usb} />
            <span className={s.optionText}>
              <Text weight="semibold" truncate wrap={false}>{d.name}</Text>
              <Caption1 className={s.muted}>{d.maker || "Unknown maker"} · {d.vid}:{d.pid}</Caption1>
            </span>
            {d.supported && <Badge appearance="tint" color="informative">Supported</Badge>}
          </button>
        ))}
      </div>
      <Switch checked={all} onChange={(_, data) => setAll(data.checked)} label="Show devices that are already supported" />
    </StepCard>
  )
}

function Countdown({ current, progress }: { current: { text: string; left: number } | null; progress: number }) {
  const s = useStyles()
  return (
    <div className={s.countdown}>
      <div className={s.countdownRow}>
        <Text size={400} weight="semibold">{current?.text ?? "Done"}</Text>
        {current && <Text className={s.muted}>{current.left}s</Text>}
      </div>
      <ProgressBar value={progress} thickness="large" shape="rounded" />
    </div>
  )
}

function Result({ r, what }: { r: Analysis; what: string }) {
  return r.found ? (
    <MessageBar intent="success">
      <MessageBarBody>
        <MessageBarTitle>Found the {what}</MessageBarTitle>
        {r.description} · {r.reports} reports heard
      </MessageBarBody>
    </MessageBar>
  ) : (
    <MessageBar intent="warning">
      <MessageBarBody>
        <MessageBarTitle>No {what} signal found</MessageBarTitle>
        Heard {r.reports} reports, none matched. Try again or skip this step.
      </MessageBarBody>
    </MessageBar>
  )
}

function BatteryStep({ onDone }: { onDone: (r: Analysis | null) => void }) {
  const s = useStyles()
  const notify = useNotify()
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
      notify((e as Error).message, "error")
    } finally {
      setRunning(false)
    }
  }

  return (
    <StepCard icon={<Battery5Regular />} title="Battery level"
              description="Enter the level the device reports right now (from its app, a voice prompt or its LEDs), then listen."
              footer={<>
                <Button appearance="subtle" disabled={running} onClick={() => onDone(null)}>Skip</Button>
                {result?.found
                  ? <Button appearance="primary" onClick={() => onDone(result)}>Next</Button>
                  : <Button appearance="primary" disabled={running} onClick={listen}
                            icon={running ? <Spinner size="tiny" /> : undefined}>
                      {result ? "Listen again" : "Listen"}
                    </Button>}
              </>}>
      <Field label="Battery now" className={s.pct}>
        <Input inputMode="numeric" value={level} placeholder="e.g. 57" contentAfter={<Text className={s.muted}>%</Text>}
               onChange={(_, d) => setLevel(d.value)} />
      </Field>
      <Switch checked={probe} onChange={(_, d) => setProbe(d.checked)}
              label="Also ask the battery questions other brands understand (read-only)" />
      {(running || result) && <Countdown current={phases.current} progress={phases.progress} />}
      {result && <Result r={result} what="battery level" />}
    </StepCard>
  )
}

function ToggleStep({ what, icon, title, description, phases: plan, onDone }: {
  what: "muted" | "charging"
  icon: ReactNode
  title: string
  description: string
  phases: Phase[]
  onDone: (r: Analysis | null) => void
}) {
  const notify = useNotify()
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
      notify((e as Error).message, "error")
    } finally {
      setRunning(false)
    }
  }

  return (
    <StepCard icon={icon} title={title} description={description}
              footer={<>
                <Button appearance="subtle" disabled={running} onClick={() => onDone(null)}>Skip</Button>
                {result?.found
                  ? <Button appearance="primary" onClick={() => onDone(result)}>Next</Button>
                  : <Button appearance="primary" disabled={running} onClick={start}
                            icon={running ? <Spinner size="tiny" /> : undefined}>
                      {result ? "Try again" : "Start"}
                    </Button>}
              </>}>
      {(running || result) && <Countdown current={phases.current} progress={phases.progress} />}
      {result && <Result r={result} what={what === "muted" ? "mute signal" : "charging signal"} />}
    </StepCard>
  )
}

const KIND_OPTIONS: Kind[] = ["headset", "mouse", "keyboard", "gamepad", "device"]
const cap = (k: string) => k[0].toUpperCase() + k.slice(1)

function SaveStep({ device, results, onRestart }: {
  device: LearnDevice
  results: Partial<Record<string, Analysis>>
  onRestart: () => void
}) {
  const s = useStyles()
  const notify = useNotify()
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
      notify(`${name} learned`, "success", "Its icon appears in the tray within a few seconds.")
    } catch (e) {
      notify((e as Error).message, "error")
    }
  }

  async function copy() {
    await navigator.clipboard.writeText(recipe)
    notify("Recipe copied", "success", "Share it with a friend who has the same device.")
  }

  return (
    <StepCard title="Name and save"
              description="This becomes a recipe. Share it and anyone with the same device gets support."
              footer={<>
                <Button appearance="subtle" onClick={onRestart}>Learn another</Button>
                <span className={s.inline}>
                  <Button icon={<CopyRegular />} disabled={!recipe} onClick={copy}>Copy</Button>
                  <Button appearance="primary" icon={<SaveRegular />} disabled={!recipe || saved !== null}
                          onClick={save}>Save</Button>
                </span>
              </>}>
      <div className={s.inline}>
        {(["level", "muted", "charging"] as const).map((k) => (
          <Badge key={k} appearance={results[k]?.found ? "filled" : "outline"}
                 color={results[k]?.found ? "brand" : "subtle"}
                 icon={results[k]?.found ? <CheckmarkRegular /> : undefined}>
            {{ level: "Battery", muted: "Mic mute", charging: "Charging" }[k]}
          </Badge>
        ))}
      </div>
      <div className={s.saveGrid}>
        <Field label="Name">
          <Input value={name} onChange={(_, d) => setName(d.value)} />
        </Field>
        <Field label="Type">
          <Dropdown value={cap(kind)} selectedOptions={[kind]}
                    onOptionSelect={(_, d) => d.optionValue && setKind(d.optionValue as Kind)}>
            {KIND_OPTIONS.map((k) => <Option key={k} value={k}>{cap(k)}</Option>)}
          </Dropdown>
        </Field>
      </div>
      <pre className={s.pre} data-testid="recipe">{recipe}</pre>
      {saved && (
        <MessageBar intent="success">
          <MessageBarBody>
            <MessageBarTitle>Saved</MessageBarTitle>
            <span className={s.code}>{saved}</span>
          </MessageBarBody>
        </MessageBar>
      )}
    </StepCard>
  )
}

function NothingFound({ onRestart }: { onRestart: () => void }) {
  const s = useStyles()
  return (
    <StepCard icon={<SearchRegular />} title="Nothing recognised"
              description="None of the steps found a usable signal. This device probably needs commands only its vendor app knows."
              footer={<Button onClick={onRestart}>Start over</Button>}>
      <Body1 className={s.muted}>
        Run <code className={s.code}>PeripheralsBattery.exe --probe</code> and share the report in an issue on
        GitHub to get it supported.
      </Body1>
    </StepCard>
  )
}
