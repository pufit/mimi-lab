import { useEffect, useState } from "react";
import * as ToastPrimitive from "@radix-ui/react-toast";
import { CheckCircle2, XCircle, Info, Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";

export type ToastVariant = "default" | "success" | "error" | "loading";

export interface ToastItem {
  id: number;
  title: string;
  description?: string;
  variant: ToastVariant;
  duration: number;
}

type Listener = (toasts: ToastItem[]) => void;

let counter = 0;
let toasts: ToastItem[] = [];
const listeners = new Set<Listener>();

function emit() {
  for (const l of listeners) l([...toasts]);
}

type ToastOpts = {
  title: string;
  description?: string;
  variant?: ToastVariant;
  duration?: number;
};

interface ToastFn {
  (opts: ToastOpts): number;
  success: (title: string, description?: string) => number;
  error: (title: string, description?: string) => number;
  loading: (title: string, description?: string) => number;
  update: (
    id: number | undefined,
    opts: { title?: string; description?: string; variant?: ToastVariant; duration?: number },
  ) => void;
  dismiss: (id: number) => void;
}

const toastBase = (opts: ToastOpts): number => {
  const id = ++counter;
  const item: ToastItem = {
    id,
    title: opts.title,
    description: opts.description,
    variant: opts.variant ?? "default",
    duration: opts.duration ?? (opts.variant === "loading" ? 1000000 : 4200),
  };
  toasts = [...toasts, item];
  emit();
  return id;
};

export const toast = toastBase as ToastFn;

export function dismissToast(id: number) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function updateToast(
  id: number | undefined,
  opts: { title?: string; description?: string; variant?: ToastVariant; duration?: number },
) {
  // `id` may be undefined when invoked from a TanStack mutation context that
  // skipped onMutate — fall back to spawning a fresh toast.
  if (id == null) {
    toastBase({ title: opts.title ?? "", description: opts.description, variant: opts.variant });
    return;
  }
  toasts = toasts.map((t) =>
    t.id === id
      ? {
          ...t,
          ...opts,
          duration:
            opts.duration ?? (opts.variant && opts.variant !== "loading" ? 4200 : t.duration),
        }
      : t,
  );
  emit();
}

toast.success = (title: string, description?: string) =>
  toast({ title, description, variant: "success" });
toast.error = (title: string, description?: string) =>
  toast({ title, description, variant: "error", duration: 6000 });
toast.loading = (title: string, description?: string) =>
  toast({ title, description, variant: "loading" });
toast.update = updateToast;
toast.dismiss = dismissToast;

const ICONS = {
  default: Info,
  success: CheckCircle2,
  error: XCircle,
  loading: Loader2,
} as const;

const ACCENT = {
  default: "var(--color-brand-bright)",
  success: "var(--color-comp-green)",
  error: "var(--color-comp-red)",
  loading: "var(--color-brand-bright)",
} as const;

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    listeners.add(setItems);
    setItems([...toasts]);
    return () => {
      listeners.delete(setItems);
    };
  }, []);

  return (
    <ToastPrimitive.Provider swipeDirection="right">
      {items.map((t) => {
        const Icon = ICONS[t.variant];
        const accent = ACCENT[t.variant];
        return (
          <ToastPrimitive.Root
            key={t.id}
            duration={t.duration}
            onOpenChange={(open) => {
              if (!open) dismissToast(t.id);
            }}
            className={cn(
              "group pointer-events-auto relative flex w-[360px] max-w-[calc(100vw-2rem)] items-start gap-3 overflow-hidden rounded-xl border bg-bg-elevated/95 p-3.5 pr-9 shadow-2xl shadow-black/50 backdrop-blur-xl",
              "data-[state=open]:animate-fade-in",
              "data-[swipe=move]:translate-x-[var(--radix-toast-swipe-move-x)] data-[swipe=cancel]:translate-x-0 data-[swipe=cancel]:transition-transform",
            )}
            style={{ borderColor: `color-mix(in oklab, ${accent} 30%, var(--color-border))` }}
          >
            <span
              className="absolute inset-y-0 left-0 w-1"
              style={{ backgroundColor: accent }}
            />
            <Icon
              className={cn("mt-0.5 size-5 shrink-0", t.variant === "loading" && "animate-spin")}
              style={{ color: accent }}
            />
            <div className="min-w-0 flex-1">
              <ToastPrimitive.Title className="text-sm font-semibold text-fg">
                {t.title}
              </ToastPrimitive.Title>
              {t.description && (
                <ToastPrimitive.Description className="mt-0.5 text-xs leading-relaxed text-muted break-words">
                  {t.description}
                </ToastPrimitive.Description>
              )}
            </div>
            <ToastPrimitive.Close
              className="absolute right-2 top-2 rounded-md p-1 text-faint transition hover:bg-surface-hover hover:text-fg"
              aria-label="Dismiss"
            >
              <X className="size-3.5" />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        );
      })}
      <ToastPrimitive.Viewport className="fixed bottom-0 right-0 z-[100] flex max-h-screen w-auto flex-col gap-2.5 p-5 outline-none" />
    </ToastPrimitive.Provider>
  );
}
