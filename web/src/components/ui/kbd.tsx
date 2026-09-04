import * as React from "react";
import { cn } from "@/lib/utils";

/** Keycap chip — used in the rating bar, the card menu and the `?` sheet. */
export function Kbd({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-grid h-5 min-w-5 place-items-center rounded-[0.3rem] border border-border-strong bg-surface px-1.5",
        "font-sans text-[0.65rem] font-semibold leading-none text-muted shadow-[inset_0_-1px_0_var(--color-border-strong)]",
        className,
      )}
      {...props}
    />
  );
}
