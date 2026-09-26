// Follow the Windows light/dark setting, live.
export function followSystemTheme() {
  const mq = window.matchMedia("(prefers-color-scheme: dark)")
  const apply = () => {
    document.documentElement.classList.toggle("dark", mq.matches)
    window.dispatchEvent(new Event("peribatt-theme"))
  }
  apply()
  mq.addEventListener("change", apply)
}
