import { useCallback, useEffect, useState } from "react"
import {
  Caption1,
  FluentProvider,
  Tab,
  TabList,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components"
import {
  AlertRegular,
  AppsListRegular,
  InfoRegular,
  PaintBrushRegular,
  PlugConnectedRegular,
  SettingsRegular,
  SparkleRegular,
  WifiOffRegular,
} from "@fluentui/react-icons"

import { NotifyProvider } from "@/components/notify"
import { useSystemTheme } from "@/components/theme"
import { api, iconUrl, type State } from "@/lib/api"
import { DevicesPage } from "@/pages/devices"
import { LearnPage } from "@/pages/learn"
import { AboutPage, AlertsPage, AppearancePage, GeneralPage, SourcesPage, useSave } from "@/pages/settings"

const NAV = [
  { id: "devices", label: "Devices", icon: <AppsListRegular /> },
  { id: "general", label: "General", icon: <SettingsRegular /> },
  { id: "alerts", label: "Alerts & colours", icon: <AlertRegular /> },
  { id: "appearance", label: "Appearance", icon: <PaintBrushRegular /> },
  { id: "sources", label: "Sources", icon: <PlugConnectedRegular /> },
  { id: "learn", label: "Learn a device", icon: <SparkleRegular /> },
  { id: "about", label: "About", icon: <InfoRegular /> },
] as const

const useStyles = makeStyles({
  shell: { display: "flex", height: "100vh", overflow: "hidden", backgroundColor: tokens.colorNeutralBackground2 },
  side: {
    display: "flex",
    flexDirection: "column",
    width: "232px",
    flexShrink: 0,
    padding: "20px 8px 16px",
    backgroundColor: tokens.colorNeutralBackground2,
  },
  brand: { display: "flex", alignItems: "center", gap: "12px", padding: "4px 12px 20px" },
  brandText: { display: "grid", lineHeight: 1.2 },
  muted: { color: tokens.colorNeutralForeground3 },
  tabs: { gap: "2px" },
  count: { marginLeft: "auto", paddingLeft: "12px", color: tokens.colorNeutralForeground3 },
  foot: { marginTop: "auto", display: "flex", alignItems: "center", gap: "6px", padding: "0 12px" },
  main: {
    flex: 1,
    overflowY: "auto",
    backgroundColor: tokens.colorNeutralBackground1,
    borderTopLeftRadius: tokens.borderRadiusXLarge,
    boxShadow: tokens.shadow4,
    margin: "8px 0 0",
  },
  content: { maxWidth: "820px", margin: "0 auto", padding: "32px 40px" },
})

function pageFromHash() {
  const [id] = window.location.hash.replace(/^#\/?/, "").split("/")
  return NAV.some((n) => n.id === id) ? id : "devices"
}

/** "#/devices/rename/<key>": the tray's Rename… item opens the dialog for that device. */
function renameFromHash() {
  const m = window.location.hash.match(/^#\/?devices\/rename\/(.+)$/)
  return m ? decodeURIComponent(m[1]) : null
}

function Shell({ dark }: { dark: boolean }) {
  const s = useStyles()
  const [page, setPage] = useState(pageFromHash)
  const [renameKey, setRenameKey] = useState(renameFromHash)
  const [state, setState] = useState<State | null>(null)
  const [offline, setOffline] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setState(await api.state())
      setOffline(false)
    } catch {
      setOffline(true)
    }
  }, [])

  // Live battery levels; the polling is also the app's sign that this window is still open.
  useEffect(() => {
    refresh()
    let timer = 0
    const loop = () => {
      timer = window.setTimeout(async () => {
        await refresh()
        loop()
      }, document.hidden ? 10000 : 2000)
    }
    loop()
    return () => window.clearTimeout(timer)
  }, [refresh])

  useEffect(() => {
    const onHash = () => {
      setPage(pageFromHash())
      setRenameKey(renameFromHash())
    }
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  const go = (id: string) => {
    window.location.hash = `/${id}`
  }
  const save = useSave(refresh)
  const standalone = state?.standalone ?? false
  const nav = standalone ? NAV.filter((n) => n.id === "learn" || n.id === "about") : NAV
  const current = standalone && page !== "about" ? "learn" : page

  return (
    <div className={s.shell} data-theme={dark ? "dark" : "light"}>
      <aside className={s.side}>
        <div className={s.brand}>
          <img src={iconUrl({ kind: "headset", level: 70, charging: true, size: 32 })} alt="" width={32} height={32} />
          <div className={s.brandText}>
            <Text weight="semibold">Peripherals Battery</Text>
            <Caption1 className={s.muted}>{state ? `v${state.version}` : " "}</Caption1>
          </div>
        </div>
        <TabList vertical size="large" selectedValue={current} className={s.tabs} aria-label="Sections"
                 onTabSelect={(_, d) => go(String(d.value))}>
          {nav.map(({ id, label, icon }) => (
            <Tab key={id} value={id} icon={icon}>
              {label}
              {id === "devices" && state && state.devices.length > 0 && (
                <span className={s.count}>{state.devices.length}</span>
              )}
            </Tab>
          ))}
        </TabList>
        {offline && (
          <Caption1 className={`${s.foot} ${s.muted}`}><WifiOffRegular /> The tray app has closed.</Caption1>
        )}
      </aside>

      <main className={s.main}>
        <div className={s.content}>
          {current === "devices" && (
            <DevicesPage state={state} refresh={refresh} go={go} renameKey={renameKey}
                         onRenameOpened={() => { setRenameKey(null); go("devices") }} />
          )}
          {current === "learn" && <LearnPage />}
          {state && current === "general" && <GeneralPage state={state} save={save} />}
          {state && current === "alerts" && <AlertsPage state={state} save={save} />}
          {state && current === "appearance" && <AppearancePage state={state} save={save} />}
          {state && current === "sources" && <SourcesPage state={state} save={save} />}
          {state && current === "about" && <AboutPage state={state} />}
        </div>
      </main>
    </div>
  )
}

export default function App() {
  const { theme, dark } = useSystemTheme()
  return (
    <FluentProvider theme={theme} style={{ height: "100%" }}>
      <NotifyProvider>
        <Shell dark={dark} />
      </NotifyProvider>
    </FluentProvider>
  )
}
