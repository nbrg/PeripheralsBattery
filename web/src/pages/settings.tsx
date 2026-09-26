import { useEffect, useRef, useState } from "react"
import {
  Body1,
  Caption1,
  Card,
  Divider,
  Dropdown,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Option,
  Slider,
  Switch,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components"
import { FolderOpenRegular } from "@fluentui/react-icons"

import { PageHeader, Section, SettingRow } from "@/components/common"
import { useNotify } from "@/components/notify"
import { BATTERY } from "@/components/theme"
import { api, iconUrl, type Kind, type Settings, type State } from "@/lib/api"

type SaveMany = (values: Partial<Settings>) => Promise<boolean>
export type Save = (<K extends keyof Settings>(key: K, value: Settings[K]) => Promise<boolean>) & { many: SaveMany }

/** Saves settings as soon as they change (no Save button to forget). */
export function useSave(refresh: () => void): Save {
  const notify = useNotify()
  const many: SaveMany = async (values) => {
    try {
      await api.saveSettings(values)
      notify("Saved")
      refresh()
      return true
    } catch (e) {
      notify((e as Error).message, "error")
      refresh()
      return false
    }
  }
  const one = ((key: keyof Settings, value: unknown) => many({ [key]: value })) as Save
  one.many = many
  return one
}

const useStyles = makeStyles({
  stack: { display: "grid", gap: "16px" },
  sub: { color: tokens.colorNeutralForeground3 },
  sliders: { display: "grid", gap: "4px", padding: "8px 0 4px" },
  sliderRow: { display: "grid", gridTemplateColumns: "150px 1fr 48px", alignItems: "center", gap: "12px" },
  value: { textAlign: "right", fontVariantNumeric: "tabular-nums" },
  preview: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "12px",
    marginTop: "12px",
    padding: "16px 20px",
    borderRadius: tokens.borderRadiusXLarge,
    backgroundColor: tokens.colorNeutralBackground3,
  },
  figure: { display: "grid", justifyItems: "center", gap: "6px", margin: 0 },
  inline: { display: "flex", alignItems: "center", gap: "12px" },
  about: { display: "grid", gap: "16px", padding: "24px" },
  aboutHead: { display: "flex", alignItems: "center", gap: "16px" },
})

function Toggle({ s, save, k, title, description }: {
  s: Settings
  save: Save
  k: keyof Settings
  title: string
  description?: string
}) {
  return (
    <SettingRow id={k} title={title} description={description}>
      <Switch id={k} checked={Boolean(s[k])} aria-label={title}
              onChange={(_, data) => save(k, data.checked as never)} />
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
  const current = options.find(([v]) => v === state.settings[k])
  return (
    <SettingRow id={k} title={title} description={description}>
      <Dropdown id={k} aria-label={title} style={{ minWidth: 150 }}
                value={current?.[1] ?? ""} selectedOptions={[String(state.settings[k])]}
                onOptionSelect={(_, data) => data.optionValue && save(k, Number(data.optionValue))}>
        {options.map(([value, label]) => <Option key={value} value={String(value)}>{label}</Option>)}
      </Dropdown>
    </SettingRow>
  )
}

// --- pages ----------------------------------------------------------------------

export function GeneralPage({ state, save }: { state: State; save: Save }) {
  const st = useStyles()
  const s = state.settings
  return (
    <>
      <PageHeader title="General" description="How often the app looks, and when it starts." />
      <div className={st.stack}>
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

const PREVIEW: [number, boolean][] = [[8, false], [25, false], [64, false], [95, false], [40, true]]
const KINDS: Kind[] = ["mouse", "headset", "keyboard", "gamepad"]

export function AlertsPage({ state, save }: { state: State; save: Save }) {
  const st = useStyles()
  const s = state.settings
  const [low, setLow] = useState(s.low)
  const [warn, setWarn] = useState(s.warn)
  const [kind, setKind] = useState<Kind>("mouse")
  const pending = useRef<number | undefined>(undefined)
  const editing = useRef(false)
  // Follow the saved values, except while the user is still moving a slider.
  useEffect(() => {
    if (!editing.current) {
      setLow(s.low)
      setWarn(s.warn)
    }
  }, [s.low, s.warn])
  useEffect(() => () => window.clearTimeout(pending.current), [])

  function change(nextLow: number, nextWarn: number) {
    // keep yellow above red whichever slider moves
    if (nextWarn <= nextLow) {
      if (nextLow !== low) nextWarn = Math.min(90, nextLow + 1)
      else nextLow = Math.max(1, nextWarn - 1)
    }
    setLow(nextLow)
    setWarn(nextWarn)
    editing.current = true
    window.clearTimeout(pending.current)
    // Dragging and arrow keys change the value many times: save once, shortly after the last change.
    pending.current = window.setTimeout(async () => {
      if (nextLow !== s.low || nextWarn !== s.warn) await save.many({ low: nextLow, warn: nextWarn })
      editing.current = false
    }, 400)
  }

  return (
    <>
      <PageHeader title="Alerts & colours" description="When the frame turns yellow or red, and when to notify you." />
      <div className={st.stack}>
        <Section title="Frame colours" description="Green while charging, red below the first limit, yellow up to the second.">
          <div className={st.sliders}>
            <div className={st.sliderRow}>
              <Text><span style={{ color: BATTERY.low, fontWeight: 600 }}>Red</span> below</Text>
              <Slider aria-label="Red below" min={1} max={50} value={low}
                      onChange={(_, data) => change(data.value, warn)} />
              <Text className={st.value}>{low}%</Text>
            </div>
            <div className={st.sliderRow}>
              <Text><span style={{ color: BATTERY.warn, fontWeight: 600 }}>Yellow</span> up to</Text>
              <Slider aria-label="Yellow up to" min={2} max={90} value={warn}
                      onChange={(_, data) => change(low, data.value)} />
              <Text className={st.value}>{warn}%</Text>
            </div>
          </div>
          <div className={st.preview} data-testid="icon-preview">
            {PREVIEW.map(([level, charging]) => (
              <figure key={`${level}${charging}`} className={st.figure}>
                <img alt="" width={40} height={40}
                     src={iconUrl({ kind, level, charging, low, warn, size: 40, number: s.show_number })} />
                <Caption1 className={st.sub}>{charging ? "charging" : `${level}%`}</Caption1>
              </figure>
            ))}
            <Dropdown aria-label="Preview device" style={{ minWidth: 130 }} value={kind[0].toUpperCase() + kind.slice(1)}
                      selectedOptions={[kind]} onOptionSelect={(_, d) => d.optionValue && setKind(d.optionValue as Kind)}>
              {KINDS.map((k) => <Option key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</Option>)}
            </Dropdown>
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
  const st = useStyles()
  const s = state.settings
  return (
    <>
      <PageHeader title="Appearance" description="What the tray icons show." />
      <div className={st.stack}>
        <Section title="Icon">
          <SettingRow id="show_number" title="Show the percentage instead of the picture"
                      description="The device picture fills up with the level; this shows the number instead.">
            <div className={st.inline}>
              <img alt="" width={32} height={32}
                   src={iconUrl({ kind: "headset", level: 64, number: s.show_number, size: 32 })} />
              <Switch id="show_number" checked={s.show_number} aria-label="Show the percentage instead of the picture"
                      onChange={(_, d) => save("show_number", d.checked)} />
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
  const st = useStyles()
  const s = state.settings
  const [hc, setHc] = useState(s.headsetcontrol)
  useEffect(() => setHc(s.headsetcontrol), [s.headsetcontrol])
  return (
    <>
      <PageHeader title="Sources" description="Where battery levels come from." />
      <div className={st.stack}>
        <Section title="Built in" description="Logitech, Razer, HyperX, SteelSeries and Corsair are always on.">
          <Toggle s={s} save={save} k="bluetooth" title="Bluetooth devices"
                  description="Anything Windows shows a battery for: keyboards like the Keychron K8 Pro, earbuds, headphones." />
          <Toggle s={s} save={save} k="xinput" title="Xbox-compatible controllers"
                  description="Xbox pads, most 2.4 GHz pads, and headsets plugged into a controller." />
        </Section>
        <Section title="HeadsetControl" description="Optional: adds 100+ more headsets.">
          <Field label="Path to headsetcontrol.exe" style={{ paddingTop: 8 }}
                 hint="Get it from github.com/Sapd/HeadsetControl. Headsets this app already reads are skipped.">
            <Input value={hc} placeholder="Empty = find it on the PATH"
                   onChange={(_, d) => setHc(d.value)}
                   onBlur={() => hc !== s.headsetcontrol && save("headsetcontrol", hc)}
                   onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
          </Field>
        </Section>
        <MessageBar intent="info">
          <MessageBarBody>
            <MessageBarTitle>Device not listed?</MessageBarTitle>
            Use Learn a device to teach the app an unsupported USB dongle in about a minute. Extra recipes live in
            recipes.json in the data folder.
          </MessageBarBody>
        </MessageBar>
      </div>
    </>
  )
}

export function AboutPage({ state }: { state: State }) {
  const st = useStyles()
  return (
    <>
      <PageHeader title="About" />
      <Card className={st.about}>
        <div className={st.aboutHead}>
          <img src={iconUrl({ kind: "headset", level: 70, charging: true, size: 64 })} alt="" width={56} height={56} />
          <div>
            <Text as="h2" size={500} weight="semibold" block style={{ margin: 0 }}>Peripherals Battery</Text>
            <Caption1 className={st.sub}>Version {state.version}</Caption1>
          </div>
        </div>
        <Divider />
        <Body1 className={st.sub}>
          Battery levels of your wireless mouse, headset, keyboard and controllers in the Windows tray - no vendor
          software needed. Open source under the MIT licence. Protocol knowledge thanks to Solaar, HeadsetControl,
          HyperHeadset and OpenRazer; the per-device tray idea was inspired by HaloBattery.
        </Body1>
        <Body1 className={st.sub} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <FolderOpenRegular /> Settings, history and recipes live in %APPDATA%\PeripheralsBattery.
        </Body1>
      </Card>
    </>
  )
}
