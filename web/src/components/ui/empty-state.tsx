import * as React from "react";
import { cn } from "@/lib/utils";

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-2xl border border-dashed border-border-strong bg-surface/30 px-8 py-16 text-center animate-fade-in",
        className,
      )}
    >
      <div className="relative mb-5">
        <div className="absolute inset-0 -z-10 rounded-full bg-brand/20 blur-2xl" />
        <div className="grid size-16 place-items-center rounded-2xl border border-border-strong bg-bg-elevated">
          <Icon className="size-7 text-brand-bright" />
        </div>
      </div>
      <h3 className="text-lg font-semibold text-fg text-balance">{title}</h3>
      {description && (
        <p className="mt-2 max-w-md text-sm leading-relaxed text-muted text-balance">
          {description}
        </p>
      )}
      {action && <div className="mt-6 flex items-center gap-3">{action}</div>}
    </div>
  );
}
