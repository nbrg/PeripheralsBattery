import { useCallback, useEffect, useState } from "react"
import {
  BellRingIcon,
  CableIcon,
  InfoIcon,
  LayoutGridIcon,
  PaletteIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  WifiOffIcon,
} from "lucide-react"

import { Toaster } from "@/components/ui/sonner"
import { api, iconUrl, type State } from "@/lib/api"
import { cn } from "@/lib/utils"
import { DevicesPage } from "@/pages/devices"
import { LearnPage } from "@/pages/learn"
import { AboutPage, AlertsPage, AppearancePage, GeneralPage, SourcesPage, useSave } from "@/pages/settings"

const NAV = [
  { id: "devices", label: "Devices", icon: LayoutGridIcon },
  { id: "general", label: "General", icon: SlidersHorizontalIcon },
  { id: "alerts", label: "Alerts & colours", icon: BellRingIcon },
  { id: "appearance", label: "Appearance", icon: PaletteIcon },
  { id: "sources", label: "Sources", icon: CableIcon },
  { id: "learn", label: "Learn a device", icon: SparklesIcon },
  { id: "about", label: "About", icon: InfoIcon },
] as const

function pageFromHash() {
  const id = window.location.hash.replace(/^#\/?/, "")
  return NAV.some((n) => n.id === id) ? id : "devices"
}

export default function App() {
  const [page, setPage] = useState(pageFromHash)
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

  // Icons are drawn for the theme: redraw them when Windows switches light/dark.
  const [, setThemeTick] = useState(0)
  useEffect(() => {
    const onTheme = () => setThemeTick((n) => n + 1)
    window.addEventListener("peribatt-theme", onTheme)
    return () => window.removeEventListener("peribatt-theme", onTheme)
  }, [])

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
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
    <div className="flex h-screen overflow-hidden">
      <aside className="bg-sidebar text-sidebar-foreground border-sidebar-border flex w-60 shrink-0 flex-col border-r">
        <div className="flex items-center gap-3 px-5 pt-6 pb-5">
          <img src={iconUrl({ kind: "headset", level: 70, charging: true, size: 32 })} alt="" className="size-8" />
          <div className="leading-tight">
            <div className="text-sm font-semibold">Peripherals Battery</div>
            <div className="text-muted-foreground text-xs">{state ? `v${state.version}` : " "}</div>
          </div>
        </div>
        <nav className="grid gap-0.5 px-3" aria-label="Sections">
          {nav.map(({ id, label, icon: Icon }) => (
            <button key={id} onClick={() => go(id)} aria-current={current === id ? "page" : undefined}
                    className={cn(
                      "flex h-9 items-center gap-3 rounded-md px-3 text-sm transition-colors",
                      current === id
                        ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                        : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground")}>
              <Icon className="size-4" /> {label}
              {id === "devices" && state && state.devices.length > 0 && (
                <span className="text-muted-foreground ml-auto text-xs tabular-nums">{state.devices.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="mt-auto p-4">
          {offline && (
            <div className="text-muted-foreground flex items-center gap-2 text-xs">
              <WifiOffIcon className="size-3.5" /> The tray app has closed.
            </div>
          )}
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-8 py-8">
          {current === "devices" && <DevicesPage state={state} refresh={refresh} go={go} />}
          {current === "learn" && <LearnPage />}
          {state && current === "general" && <GeneralPage state={state} save={save} />}
          {state && current === "alerts" && <AlertsPage state={state} save={save} />}
          {state && current === "appearance" && <AppearancePage state={state} save={save} />}
          {state && current === "sources" && <SourcesPage state={state} save={save} />}
          {state && current === "about" && <AboutPage state={state} />}
        </div>
      </main>
      <Toaster position="bottom-right" />
    </div>
  )
}
