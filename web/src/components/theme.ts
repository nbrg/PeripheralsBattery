import { useEffect, useState } from "react"
import { webDarkTheme, webLightTheme, type Theme } from "@fluentui/react-components"

// Status colours shared by the battery bars and badges; the same rules as the tray icon frame.
export const BATTERY = {
  charging: "#2ecc71",
  low: "#e74c3c",
  warn: "#f1c40f",
}

/** Follows the Windows light/dark setting, live. */
export function useSystemTheme(): { theme: Theme; dark: boolean } {
  const mq = window.matchMedia("(prefers-color-scheme: dark)")
  const [dark, setDark] = useState(mq.matches)
  useEffect(() => {
    const onChange = () => setDark(mq.matches)
    mq.addEventListener("change", onChange)
    return () => mq.removeEventListener("change", onChange)
  }, [mq])
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light"
    document.documentElement.style.colorScheme = dark ? "dark" : "light"
  }, [dark])
  return { theme: dark ? webDarkTheme : webLightTheme, dark }
}
