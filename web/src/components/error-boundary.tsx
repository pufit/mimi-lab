import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./ui/button";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/**
 * Top-level error boundary. Without this, any render-time throw unmounts the
 * whole tree and leaves a permanent white screen below #root. Here we show a
 * recoverable fallback with the error message and a reload instead.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface it in the console for debugging; the fallback handles the UI.
    console.error("Unhandled render error:", error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-8 text-center">
          <div className="max-w-lg rounded-2xl border border-comp-red/25 bg-comp-red/5 px-8 py-10">
            <p className="text-base font-semibold text-fg">Something broke on this screen</p>
            <p className="mt-2 text-sm text-muted">
              The page hit an unexpected error. Your data is safe — this is a display glitch.
            </p>
            <pre className="mt-4 max-h-40 overflow-auto rounded-lg bg-black/30 p-3 text-left text-[11px] text-comp-red/90">
              {this.state.error.message}
            </pre>
            <div className="mt-5 flex justify-center gap-3">
              <Button variant="secondary" size="sm" onClick={this.reset}>
                Try again
              </Button>
              <Button size="sm" onClick={() => window.location.reload()}>
                Reload app
              </Button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
