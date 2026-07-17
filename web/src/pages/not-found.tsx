import { Link } from "react-router-dom";
import { Compass } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";

export function NotFoundPage() {
  return (
    <div className="animate-fade-in pt-10">
      <EmptyState
        icon={Compass}
        title="Page not found"
        description="That route doesn't exist. Let's get you back to your library."
        action={
          <Button variant="primary" asChild>
            <Link to="/">Back to Library</Link>
          </Button>
        }
      />
    </div>
  );
}
