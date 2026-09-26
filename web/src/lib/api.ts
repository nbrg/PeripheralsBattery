// Tiny client for the tray app's local API (see peribatt/web.py).

const params = new URLSearchParams(window.location.search)
const token = params.get("t") ?? sessionStorage.getItem("peribatt-token") ?? ""
if (token) sessionStorage.setItem("peribatt-token", token)

export type Kind = "mouse" | "headset" | "keyboard" | "gamepad" | "device"

export interface Device {
  key: string
  name: string
  original: string
  kind: Kind
  level: number | null
  charging: boolean
  online: boolean
  muted: boolean
  note: string
  hidden: boolean
  estimate: string | null
  icon: string
}

export interface Settings {
  poll_seconds: number
  alert_at: number
  notify_full: boolean
  autostart: boolean
  history: boolean
  low: number
  warn: number
  show_number: boolean
  flash_on_mute: boolean
  windows_mute: boolean
  bluetooth: boolean
  xinput: boolean
  headsetcontrol: string
}

export interface State {
  version: string
  platform: string
  standalone: boolean
  choices: Record<string, [number, string][]>
  limits: Record<string, [number, string]>
  devices: Device[]
  settings: Settings
}

export class ApiError extends Error {}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "X-Token": token, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const msg = (data.errors as string[] | undefined)?.join("\n") ?? data.error ?? res.statusText
    throw new ApiError(msg)
  }
  return data as T
}

export const api = {
  state: () => call<State>("GET", "/api/state"),
  saveSettings: (values: Partial<Settings>) =>
    call<{ ok: boolean; settings: Settings }>("POST", "/api/settings", values),
  device: (key: string, action: "rename" | "hide" | "show" | "forget", name?: string) =>
    call<{ ok: boolean }>("POST", "/api/device", { key, action, name }),
  learn: {
    devices: (all: boolean) => call<LearnDevice[]>("GET", `/api/learn/devices?all=${all ? 1 : 0}`),
    open: (id: number) => call<{ ok: boolean; collections: number }>("POST", "/api/learn/open", { id }),
    phase: (phase: string) => call<{ ok: boolean }>("POST", "/api/learn/phase", { phase }),
    queries: () => call<{ ok: boolean }>("POST", "/api/learn/queries", {}),
    analyze: (what: "level" | "muted" | "charging", level?: number) =>
      call<Analysis>("POST", "/api/learn/analyze", { what, level }),
    recipe: (name: string, kind: Kind) => call<{ recipe: object }>("POST", "/api/learn/recipe", { name, kind }),
    save: (name: string, kind: Kind) =>
      call<{ ok: boolean; path: string; recipe: object }>("POST", "/api/learn/save", { name, kind }),
    close: () => call<{ ok: boolean }>("POST", "/api/learn/close", {}),
  },
}

export interface LearnDevice {
  id: number
  name: string
  maker: string
  vid: string
  pid: string
  supported: boolean
  collections: number
}

export interface Analysis {
  found: boolean
  reports: number
  description: string
}

/** Icon preview URL; the image itself is drawn by the app, so it matches the tray exactly. */
export function iconUrl(p: {
  kind: Kind
  level: number | null
  charging?: boolean
  online?: boolean
  low?: number
  warn?: number
  number?: boolean
  size?: number
}) {
  const q = new URLSearchParams({
    kind: p.kind,
    level: p.level === null ? "" : String(p.level),
    charging: p.charging ? "1" : "0",
    online: p.online === false ? "0" : "1",
    size: String(p.size ?? 64),
    number: p.number ? "1" : "0",
  })
  if (p.low !== undefined) q.set("low", String(p.low))
  if (p.warn !== undefined) q.set("warn", String(p.warn))
  return withToken(`/api/icon?${q}`)
}

/** Adds the session token (images can't send headers) and the page theme, so
 *  icons are drawn dark-on-light or light-on-dark like on a real taskbar. */
export function withToken(url: string) {
  const light = document.documentElement.classList.contains("dark") ? "0" : "1"
  return `${url}&light=${light}&t=${encodeURIComponent(token)}`
}
