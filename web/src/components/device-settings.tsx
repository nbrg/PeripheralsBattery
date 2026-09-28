import { useEffect, useRef, useState } from "react"
import {
  Button,
  Caption1,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Dropdown,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  Option,
  Slider,
  SpinButton,
  Spinner,
  Switch,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components"
import { ArrowClockwiseRegular } from "@fluentui/react-icons"

import { useNotify } from "@/components/notify"
import { api, type Control, type Device } from "@/lib/api"

/** Common DPI steps, offered as one-click presets when the mouse supports them. */
const DPI_PRESETS = [400, 800, 1600, 3200, 6400]
/** A slider change is written once the user stops moving it for this long. */
const SETTLE_MS = 450
/** Slider resolution when a wide range (DPI 100..25600) is shown on a log scale. */
const LOG_STEPS = 1000

const useStyles = makeStyles({
  surface: { maxWidth: "520px", width: "100%" },
  list: { display: "grid", gap: "4px" },
  row: {
    display: "grid",
    gap: "10px",
    padding: "16px 0",
    borderTop: `1px solid ${tokens.colorNeutralStroke2}`,
    ":first-of-type": { borderTop: "none", paddingTop: "4px" },
  },
  head: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "16px" },
  label: { display: "flex", alignItems: "center", gap: "8px" },
  help: { color: tokens.colorNeutralForeground3 },
  rangeLine: { display: "grid", gridTemplateColumns: "1fr 130px", alignItems: "center", gap: "12px" },
  presets: { display: "flex", flexWrap: "wrap", gap: "6px" },
  loading: { display: "grid", placeItems: "center", padding: "40px 0" },
  empty: { padding: "12px 0", color: tokens.colorNeutralForeground3 },
})

export function DeviceSettingsDialog({ device, onClose }: { device: Device | null; onClose: () => void }) {
  const s = useStyles()
  const notify = useNotify()
  const [controls, setControls] = useState<Control[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  const key = device?.key

  async function load() {
    if (!key) return
    setControls(null)
    setError(null)
    try {
      setControls((await api.controls(key)).controls)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  async function save(c: Control, value: number | boolean) {
    if (!key) return
    setSaving(c.id)
    try {
      setControls((await api.setControl(key, c.id, value)).controls)
      notify(`${c.label} saved`)
    } catch (e) {
      notify((e as Error).message, "error")
      void load() // show what the device really has now
    } finally {
      setSaving(null)
    }
  }

  return (
    <Dialog open={device !== null} onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface className={s.surface}>
        <DialogBody>
          <DialogTitle>{device?.name} settings</DialogTitle>
          <DialogContent>
            {error !== null ? (
              <MessageBar intent="warning">
                <MessageBarBody>{error}</MessageBarBody>
                <MessageBarActions>
                  <Button size="small" icon={<ArrowClockwiseRegular />} onClick={() => void load()}>Try again</Button>
                </MessageBarActions>
              </MessageBar>
            ) : controls === null ? (
              <div className={s.loading}><Spinner label="Asking the device…" /></div>
            ) : controls.length === 0 ? (
              <Text className={s.empty}>This device has no settings the app can change.</Text>
            ) : (
              <div className={s.list} data-testid="device-settings">
                {controls.map((c) => (
                  <div key={c.id} className={s.row}>
                    <div className={s.head}>
                      <span className={s.label}>
                        <Text weight="semibold">{c.label}</Text>
                        {saving === c.id && <Spinner size="extra-tiny" aria-label="Saving" />}
                      </span>
                      {c.type === "toggle" && (
                        <Switch checked={c.value === true} aria-label={c.label}
                                label={c.value === null ? "Not known yet" : undefined}
                                onChange={(_, d) => void save(c, d.checked)} />
                      )}
                      {c.type === "choice" && (
                        <Dropdown aria-label={c.label} style={{ minWidth: 170 }}
                                  value={c.options?.find(([v]) => v === c.value)?.[1] ?? ""}
                                  selectedOptions={c.value === null ? [] : [String(c.value)]}
                                  onOptionSelect={(_, d) => d.optionValue && void save(c, Number(d.optionValue))}>
                          {c.options?.map(([v, label]) => <Option key={v} value={String(v)}>{label}</Option>)}
                        </Dropdown>
                      )}
                    </div>
                    {c.type === "range" && <RangeControl c={c} onCommit={(v) => void save(c, v)} />}
                    {c.help && <Caption1 className={s.help}>{c.help}</Caption1>}
                  </div>
                ))}
              </div>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="primary" onClick={onClose}>Done</Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}

function RangeControl({ c, onCommit }: { c: Control; onCommit: (v: number) => void }) {
  const s = useStyles()
  const min = c.min ?? 0
  const max = c.max ?? 100
  const step = c.step ?? 1
  const [value, setValue] = useState(Number(c.value ?? min))
  const timer = useRef<number | undefined>(undefined)

  // The device's answer wins over what is on screen.
  useEffect(() => setValue(Number(c.value ?? min)), [c.value, min])
  useEffect(() => () => window.clearTimeout(timer.current), [])

  function change(v: number, now = false) {
    const snapped = Math.min(max, Math.max(min, min + Math.round((v - min) / step) * step))
    setValue(snapped)
    window.clearTimeout(timer.current)
    if (snapped === c.value) return
    if (now) onCommit(snapped)
    else timer.current = window.setTimeout(() => onCommit(snapped), SETTLE_MS)
  }

  // DPI from 100 to 25600: on a linear slider the useful 400..3200 would be a sliver at
  // the left end, so a wide range is laid out logarithmically. No `step` on the Slider:
  // Fluent draws a tick per step, and hundreds of them paint over the whole track;
  // change() snaps to the device's step instead.
  const log = min > 0 && max / min >= 20
  const toPos = (v: number) => (log ? Math.round((LOG_STEPS * Math.log(v / min)) / Math.log(max / min)) : v)
  const fromPos = (p: number) => (log ? min * Math.pow(max / min, p / LOG_STEPS) : p)

  const presets = c.id === "dpi" ? DPI_PRESETS.filter((p) => p >= min && p <= max) : []
  return (
    <>
      <div className={s.rangeLine}>
        <Slider aria-label={c.label} aria-valuetext={`${value} ${c.unit}`}
                min={log ? 0 : min} max={log ? LOG_STEPS : max} value={toPos(value)}
                onChange={(_, d) => change(fromPos(d.value))} />
        <SpinButton aria-label={`${c.label} value`} min={min} max={max} step={step}
                    value={value} displayValue={`${value}${c.unit ? ` ${c.unit}` : ""}`}
                    onChange={(_, d) => {
                      const v = d.value ?? Number.parseInt(d.displayValue ?? "", 10)
                      if (Number.isFinite(v)) change(v as number, true)
                    }} />
      </div>
      {presets.length > 0 && (
        <div className={s.presets}>
          {presets.map((p) => (
            <Button key={p} size="small" shape="circular" appearance={p === value ? "primary" : "secondary"}
                    onClick={() => change(p, true)}>{p}</Button>
          ))}
        </div>
      )}
    </>
  )
}
