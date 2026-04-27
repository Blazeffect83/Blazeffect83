"use client";

import { useEffect, useRef, useState } from "react";
import { loadJSON, saveJSON } from "./storage";

export type SyncStatus = "idle" | "loading" | "synced" | "offline" | "saving";

const STATE_KEYS = ["workout-draft", "meals", "progress-entries"] as const;
type StateKey = (typeof STATE_KEYS)[number];

/**
 * useSyncedState — local-first state with optimistic server sync.
 *
 * Boot order:
 *   1. Hydrate from localStorage immediately (so the UI never blocks on the network).
 *   2. Fetch from /api/state/<key>; if it differs, update both state and localStorage.
 *
 * Updates:
 *   1. Set local state instantly.
 *   2. Mirror to localStorage.
 *   3. Debounced PUT to /api/state/<key>; flip status badge so the user can see save state.
 *
 * If the server is unreachable we stay on the localStorage copy and surface "offline".
 */
export function useSyncedState<T>(key: StateKey, fallback: T) {
  const [value, setValue] = useState<T>(() => loadJSON<T>(`vf:${key}`, fallback));
  const [status, setStatus] = useState<SyncStatus>("loading");
  const hydrated = useRef(false);
  const writeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Initial hydrate from server.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/state/${key}`, { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = (await r.json()) as { value: T | null };
        if (cancelled) return;
        if (body.value !== null && body.value !== undefined) {
          setValue(body.value);
          saveJSON(`vf:${key}`, body.value);
        }
        setStatus("synced");
      } catch {
        if (!cancelled) setStatus("offline");
      } finally {
        hydrated.current = true;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [key]);

  // Debounced write-through after hydration.
  useEffect(() => {
    if (!hydrated.current) return;
    saveJSON(`vf:${key}`, value);

    if (writeTimer.current) clearTimeout(writeTimer.current);
    setStatus("saving");
    writeTimer.current = setTimeout(async () => {
      try {
        const r = await fetch(`/api/state/${key}`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(value),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        setStatus("synced");
      } catch {
        setStatus("offline");
      }
    }, 400);

    return () => {
      if (writeTimer.current) clearTimeout(writeTimer.current);
    };
  }, [key, value]);

  return [value, setValue, status] as const;
}

/**
 * Upload a photo file to the Pi. Returns a URL the <img> tag can use.
 * On failure, falls back to a transient object URL so the UX still works.
 */
export async function uploadPhoto(file: File): Promise<string> {
  try {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("/api/photos", { method: "POST", body: fd });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = (await r.json()) as { url: string };
    return body.url;
  } catch {
    return URL.createObjectURL(file);
  }
}
