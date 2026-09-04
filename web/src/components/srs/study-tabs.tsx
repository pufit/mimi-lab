import { NavLink } from "react-router-dom";
import { BarChart3, GraduationCap, Layers } from "lucide-react";
import { cn } from "@/lib/utils";

const TABS = [
  { to: "/study", label: "Deck", icon: GraduationCap, end: true },
  { to: "/study/stack", label: "Stack", icon: Layers, end: false },
  { to: "/study/stats", label: "Stats", icon: BarChart3, end: false },
];

/**
 * The strip under the page header on every non-review Study page. A card
 * detail page counts as part of the Stack, which is why `/study` matches
 * exactly and the others don't.
 */
export function StudyTabs({ className }: { className?: string }) {
  return (
    <nav
      aria-label="Study sections"
      className={cn(
        "mb-6 inline-flex items-center gap-1 rounded-xl border border-border bg-bg-elevated/60 p-1",
        className,
      )}
    >
      {TABS.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            cn(
              "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/70",
              isActive
                ? "bg-brand/15 text-fg"
                : "text-muted hover:bg-surface-hover hover:text-fg",
            )
          }
        >
          {({ isActive }) => (
            <>
              <Icon className={cn("size-4", isActive ? "text-brand-bright" : "text-faint")} />
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}
