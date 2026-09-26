import { createContext, useContext, type ReactNode } from "react"
import {
  Toast,
  ToastBody,
  ToastTitle,
  Toaster,
  useId,
  useToastController,
  type ToastIntent,
} from "@fluentui/react-components"

type Notify = (title: string, intent?: ToastIntent, body?: string) => void

const Ctx = createContext<Notify>(() => {})

/** Toast notifications ("Saved", errors) available anywhere below. */
export function NotifyProvider({ children }: { children: ReactNode }) {
  const toasterId = useId("toaster")
  const { dispatchToast } = useToastController(toasterId)
  const notify: Notify = (title, intent = "success", body) =>
    dispatchToast(
      <Toast>
        <ToastTitle>{title}</ToastTitle>
        {body && <ToastBody>{body}</ToastBody>}
      </Toast>,
      { intent, timeout: intent === "error" ? 5000 : 1600, toastId: intent === "success" && title === "Saved" ? "saved" : undefined },
    )
  return (
    <Ctx.Provider value={notify}>
      {children}
      <Toaster toasterId={toasterId} position="bottom-end" />
    </Ctx.Provider>
  )
}

export const useNotify = () => useContext(Ctx)
