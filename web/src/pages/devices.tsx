import { useState } from "react"
import {
  Badge,
  Body1,
  Button,
  Caption1,
  Card,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Menu,
  MenuDivider,
  MenuItem,
  MenuItemRadio,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Skeleton,
  SkeletonItem,
  Subtitle2,
  Text,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components"
import {
  BatteryChargeRegular,
  DeleteRegular,
  EditRegular,
  EyeOffRegular,
  EyeRegular,
  ImageRegular,
  MicOffRegular,
  MoreHorizontalRegular,
  PlugDisconnectedRegular,
  PowerRegular,
  SettingsRegular,
  SparkleRegular,
} from "@fluentui/react-icons"

import { BatteryBar, PageHeader } from "@/components/common"
import { DeviceSettingsDialog } from "@/components/device-settings"
import { useNotify } from "@/components/notify"
import { api, withToken, type Device, type Kind, type State } from "@/lib/api"
import { UpdateBar } from "@/pages/settings"

const PICTURES: ["" | Kind, string][] = [
  ["", "Automatic"], ["mouse", "Mouse"], ["keyboard", "Keyboard"],
  ["headset", "Headset"], ["gamepad", "Controller"], ["device", "Other"],
]

const KIND_LABEL: Record<string, string> = {
  mouse: "Mouse",
  headset: "Headset",
  keyboard: "Keyboard",
  gamepad: "Controller",
  device: "Device",
}

const useStyles = makeStyles({
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(290px, 1fr))", gap: "16px" },
  card: { padding: "20px", gap: "18px" },
  dim: { opacity: 0.6 },
  top: { display: "flex", alignItems: "flex-start", gap: "16px" },
  iconTile: {
    display: "grid",
    placeItems: "center",
    width: "56px",
    height: "56px",
    flexShrink: 0,
    borderRadius: tokens.borderRadiusXLarge,
    backgroundColor: tokens.colorNeutralBackground3,
  },
  titleBox: { minWidth: 0, flex: 1, display: "grid", gap: "2px" },
  name: {
    display: "-webkit-box",
    WebkitLineClamp: "2",
    WebkitBoxOrient: "vertical",
    overflow: "hidden",
    lineHeight: tokens.lineHeightBase400,
  },
  sub: { color: tokens.colorNeutralForeground3 },
  badges: { display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" },
  levelRow: { display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: "10px" },
  level: { fontSize: "32px", lineHeight: "36px", fontWeight: tokens.fontWeightSemibold, letterSpacing: "-0.5px" },
  empty: {
    display: "grid",
    justifyItems: "center",
    gap: "12px",
    padding: "56px 24px",
    textAlign: "center",
    border: `1px dashed ${tokens.colorNeutralStroke1}`,
    borderRadius: tokens.borderRadiusXLarge,
  },
  emptyIcon: { fontSize: "40px", color: tokens.colorNeutralForeground3 },
  footer: { display: "flex", justifyContent: "flex-end", marginTop: "-6px" },
})

export function DevicesPage({ state, refresh, go }: {
  state: State | null
  refresh: () => void
  go: (page: string) => void
}) {
  const s = useStyles()
  const notify = useNotify()
  const [renaming, setRenaming] = useState<Device | null>(null)
  const [forgetting, setForgetting] = useState<Device | null>(null)
  const [configuring, setConfiguring] = useState<Device | null>(null)

  async function picture(d: Device, kind: "" | Kind) {
    try {
      await api.picture(d.key, kind)
      notify(kind ? `${d.name} now shows a ${PICTURES.find(([k]) => k === kind)?.[1].toLowerCase()}` :
             `${d.name} is back to its own picture`)
      refresh()
    } catch (e) {
      notify((e as Error).message, "error")
    }
  }

  async function act(d: Device, action: "hide" | "show" | "forget", done: string) {
    try {
      await api.device(d.key, action)
      notify(done)
      refresh()
    } catch (e) {
      notify((e as Error).message, "error")
    }
  }

  const devices = state?.devices ?? []
  return (
    <>
      <PageHeader
        title="Devices"
        description="Everything the app has found. Each one gets its own tray icon."
        actions={<Button icon={<SparkleRegular />} onClick={() => go("learn")}>Learn a new device</Button>}
      />

      {state?.update && <div style={{ marginBottom: 16 }}><UpdateBar update={state.update} /></div>}
      {state === null ? (
        <Skeleton className={s.grid} aria-label="Loading devices">
          <SkeletonItem style={{ height: 150, borderRadius: 12 }} />
          <SkeletonItem style={{ height: 150, borderRadius: 12 }} />
        </Skeleton>
      ) : devices.length === 0 ? (
        <div className={s.empty}>
          <PlugDisconnectedRegular className={s.emptyIcon} />
          <Subtitle2>No devices yet</Subtitle2>
          <Body1 className={s.sub}>
            Switch your wireless gear on and plug in its receiver. Bluetooth devices appear as soon as Windows
            shows their battery.
          </Body1>
          <Button icon={<SparkleRegular />} onClick={() => go("learn")}>Learn a new device</Button>
        </div>
      ) : (
        <div className={s.grid} data-testid="device-grid">
          {devices.map((d) => (
            <DeviceCard key={d.key} d={d} low={state.settings.low} warn={state.settings.warn}
                        onRename={() => setRenaming(d)}
                        onConfigure={() => setConfiguring(d)}
                        onPicture={(kind) => picture(d, kind)}
                        onToggle={() => act(d, d.hidden ? "show" : "hide",
                                            d.hidden ? `${d.name} is back in the tray` : `${d.name} hidden from the tray`)}
                        onForget={() => setForgetting(d)} />
          ))}
        </div>
      )}

      <RenameDialog device={renaming} onClose={() => setRenaming(null)} onDone={refresh} />
      <DeviceSettingsDialog device={configuring} onClose={() => setConfiguring(null)} />

      <Dialog open={forgetting !== null} onOpenChange={(_, data) => !data.open && setForgetting(null)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Forget {forgetting?.name}?</DialogTitle>
            <DialogContent>
              Its grey icon and remembered battery level are removed. It comes back automatically the next time
              it is switched on.
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setForgetting(null)}>Cancel</Button>
              <Button appearance="primary" onClick={() => {
                if (forgetting) act(forgetting, "forget", `${forgetting.name} forgotten`)
                setForgetting(null)
              }}>Forget</Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </>
  )
}

function DeviceCard({ d, low, warn, onRename, onConfigure, onPicture, onToggle, onForget }: {
  d: Device
  low: number
  warn: number
  onRename: () => void
  onConfigure: () => void
  onPicture: (kind: "" | Kind) => void
  onToggle: () => void
  onForget: () => void
}) {
  const s = useStyles()
  return (
    <Card className={mergeClasses(s.card, d.hidden && s.dim)} data-testid="device-card">
      <div className={s.top}>
        <div className={s.iconTile}>
          <img src={withToken(`${d.icon}&size=48`)} alt="" width={40} height={40} />
        </div>
        <div className={s.titleBox}>
          <Text as="h3" weight="semibold" size={400} className={s.name} title={d.name} style={{ margin: 0 }}>
            {d.name}
          </Text>
          <Caption1 className={s.sub}>
            {KIND_LABEL[d.kind] ?? "Device"}{d.name !== d.original && <> · {d.original}</>}
          </Caption1>
          <div className={s.badges}>
            {!d.online && <Badge appearance="outline" color="subtle" icon={<PowerRegular />}>{d.note || "Off"}</Badge>}
            {d.online && d.charging && (
              <Badge appearance="tint" color="success" icon={<BatteryChargeRegular />}>Charging</Badge>
            )}
            {d.online && d.muted && <Badge appearance="filled" color="danger" icon={<MicOffRegular />}>Mic muted</Badge>}
            {d.hidden && <Badge appearance="tint" color="informative" icon={<EyeOffRegular />}>Hidden</Badge>}
          </div>
        </div>
        <Menu positioning="below-end">
          <MenuTrigger disableButtonEnhancement>
            <Button appearance="subtle" icon={<MoreHorizontalRegular />} aria-label={`Actions for ${d.name}`} />
          </MenuTrigger>
          <MenuPopover>
            <MenuList>
              {d.configurable && (
                <MenuItem icon={<SettingsRegular />} onClick={onConfigure}>Device settings</MenuItem>
              )}
              <MenuItem icon={<EditRegular />} onClick={onRename}>Rename</MenuItem>
              <Menu checkedValues={{ picture: [d.picture] }}
                    onCheckedValueChange={(_, data) => onPicture(data.checkedItems[0] as "" | Kind)}>
                <MenuTrigger disableButtonEnhancement>
                  <MenuItem icon={<ImageRegular />}>Picture</MenuItem>
                </MenuTrigger>
                <MenuPopover>
                  <MenuList>
                    {PICTURES.map(([kind, label]) => (
                      <MenuItemRadio key={kind || "auto"} name="picture" value={kind}>{label}</MenuItemRadio>
                    ))}
                  </MenuList>
                </MenuPopover>
              </Menu>
              <MenuItem icon={d.hidden ? <EyeRegular /> : <EyeOffRegular />} onClick={onToggle}>
                {d.hidden ? "Show in tray" : "Hide from tray"}
              </MenuItem>
              <MenuDivider />
              <MenuItem icon={<DeleteRegular />} disabled={d.online} onClick={onForget}>Forget</MenuItem>
            </MenuList>
          </MenuPopover>
        </Menu>
      </div>
      <div>
        <div className={s.levelRow}>
          <span className={s.level}>
            {d.level === null ? "–" : `${d.note === "approximate" ? "~" : ""}${d.level}%`}
          </span>
          <Caption1 className={s.sub}>
            {d.online ? d.estimate ?? (d.charging ? "Charging" : "") : "Last known level"}
          </Caption1>
        </div>
        <BatteryBar level={d.level} charging={d.charging} online={d.online} low={low} warn={warn} />
      </div>
      {d.configurable && (
        <div className={s.footer}>
          <Button appearance="subtle" size="small" icon={<SettingsRegular />} onClick={onConfigure}
                  disabled={!d.online} title={d.online ? undefined : "Switch the device on to change its settings"}>
            {d.kind === "mouse" ? "DPI & polling rate" : "Device settings"}
          </Button>
        </div>
      )}
    </Card>
  )
}

function RenameDialog({ device, onClose, onDone }: {
  device: Device | null
  onClose: () => void
  onDone: () => void
}) {
  const notify = useNotify()
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
      notify(name.trim() ? `Renamed to ${name.trim()}` : `Back to ${device.original}`)
      onDone()
      onClose()
    } catch (err) {
      notify((err as Error).message, "error")
    }
  }

  return (
    <Dialog open={device !== null} onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface>
        <form onSubmit={save}>
          <DialogBody>
            <DialogTitle>Rename device</DialogTitle>
            <DialogContent>
              <Field label="Device name" hint={`Shown in the tooltip and notifications. Leave empty to use “${device?.original}”.`}>
                <Input autoFocus value={name} maxLength={60} placeholder={device?.original}
                       onChange={(_, data) => setName(data.value)} />
              </Field>
            </DialogContent>
            <DialogActions>
              <Button onClick={onClose}>Cancel</Button>
              <Button type="submit" appearance="primary">Save</Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  )
}
