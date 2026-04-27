"use client";

import { Cloud, CloudOff, Loader2 } from "lucide-react";
import type { SyncStatus } from "@/lib/sync";

export function SyncBadge({ status }: { status: SyncStatus }) {
  const map: Record<SyncStatus, { label: string; icon: React.ReactNode; tone: string }> = {
    idle: { label: "Idle", icon: <Cloud className="h-3 w-3" />, tone: "text-bone-300" },
    loading: { label: "Loading", icon: <Loader2 className="h-3 w-3 animate-spin" />, tone: "text-bone-300" },
    synced: { label: "Synced", icon: <Cloud className="h-3 w-3" />, tone: "text-maroon-300" },
    saving: { label: "Saving", icon: <Loader2 className="h-3 w-3 animate-spin" />, tone: "text-bone-100" },
    offline: { label: "Local only", icon: <CloudOff className="h-3 w-3" />, tone: "text-bone-300" },
  };
  const m = map[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 border border-bone-50/10 bg-ink-900 px-2 py-1 font-mono text-[10px] uppercase tracking-widest ${m.tone}`}
      role="status"
      aria-live="polite"
    >
      {m.icon} {m.label}
    </span>
  );
}
