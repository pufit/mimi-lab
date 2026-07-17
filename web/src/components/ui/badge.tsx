import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[0.7rem] font-medium leading-none transition-colors",
  {
    variants: {
      variant: {
        default: "border-border-strong bg-surface text-muted",
        solid: "border-transparent bg-brand/20 text-brand-bright",
        success:
          "border-comp-green/30 bg-comp-green/15 text-comp-green",
        warning:
          "border-comp-amber/30 bg-comp-amber/15 text-comp-amber",
        danger: "border-comp-red/30 bg-comp-red/15 text-comp-red",
        outline: "border-border-strong bg-transparent text-faint",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
