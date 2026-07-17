import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-all duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 disabled:pointer-events-none disabled:opacity-50 active:scale-[0.98] select-none",
  {
    variants: {
      variant: {
        primary:
          "bg-brand text-white shadow-lg shadow-brand/25 hover:bg-brand-bright hover:shadow-brand/40",
        secondary:
          "border border-border-strong bg-surface text-fg hover:bg-surface-hover hover:border-faint",
        ghost: "text-muted hover:bg-surface-hover hover:text-fg",
        outline:
          "border border-border-strong bg-transparent text-fg hover:bg-surface-hover",
        danger:
          "bg-comp-red/90 text-white hover:bg-comp-red shadow-lg shadow-comp-red/20",
        play: "bg-gradient-to-r from-brand to-brand-dim text-white shadow-lg shadow-brand/30 hover:shadow-brand/50 hover:from-brand-bright hover:to-brand font-semibold",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-9 px-4",
        lg: "h-11 px-6 text-[0.95rem]",
        icon: "size-9 p-0",
        "icon-sm": "size-8 p-0",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading, children, disabled, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    // When asChild, Slot requires a SINGLE child — so we must not emit the
    // loading spinner as a sibling (Radix Slot ≥1.2 throws "Expected a single
    // React element child"). asChild buttons are links and don't use `loading`.
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, className }))}
        disabled={disabled || loading}
        {...props}
      >
        {asChild ? (
          children
        ) : (
          <>
            {loading && <Loader2 className="size-4 animate-spin" />}
            {children}
          </>
        )}
      </Comp>
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
