import { AlertTriangle } from "lucide-react";
import { Button } from "./button";

export function ErrorState({
  message,
  onRetry,
}: {
  message?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-comp-red/25 bg-comp-red/5 px-8 py-12 text-center">
      <AlertTriangle className="mb-3 size-7 text-comp-red" />
      <p className="text-sm font-medium text-fg">Something went wrong</p>
      <p className="mt-1 max-w-md text-xs text-muted">{message ?? "Failed to load."}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" className="mt-4" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
