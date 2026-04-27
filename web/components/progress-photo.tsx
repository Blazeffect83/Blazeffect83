"use client";

import { useState } from "react";
import { ImageIcon, Plus, Scale, Trash2 } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { SyncBadge } from "./sync-badge";
import { useSyncedState, uploadPhoto } from "@/lib/sync";
import { formatDate } from "@/lib/utils";

type Entry = {
  id: string;
  date: string;
  weight: number;
  notes?: string;
  photoDataUrl?: string;
};

const SEED: Entry[] = [
  { id: "p1", date: offset(-28), weight: 184.6, notes: "End of cut block 1." },
  { id: "p2", date: offset(-21), weight: 185.2 },
  { id: "p3", date: offset(-14), weight: 184.0, notes: "Sleep was rough this week." },
  { id: "p4", date: offset(-7), weight: 183.4, notes: "Carbs +30g on heavy days." },
];

export function ProgressJournal() {
  const [entries, setEntries, syncStatus] = useSyncedState<Entry[]>("progress-entries", SEED);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Entry>({
    id: "",
    date: new Date().toISOString().slice(0, 10),
    weight: 0,
    notes: "",
  });
  const [compare, setCompare] = useState<{ a: string | null; b: string | null }>({
    a: null,
    b: null,
  });

  const onPhoto = async (file: File) => {
    const url = await uploadPhoto(file);
    setDraft((d) => ({ ...d, photoDataUrl: url }));
  };

  const save = () => {
    if (!draft.weight) return;
    const entry: Entry = { ...draft, id: `p${Date.now()}` };
    setEntries((e: Entry[]) => [entry, ...e]);
    setOpen(false);
    setDraft({ id: "", date: new Date().toISOString().slice(0, 10), weight: 0, notes: "" });
  };

  const remove = (id: string) => setEntries((e: Entry[]) => e.filter((x) => x.id !== id));

  const a = entries.find((e) => e.id === compare.a) ?? entries[0];
  const b = entries.find((e) => e.id === compare.b) ?? entries[entries.length - 1];

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">/ Progress journal</p>
          <h1 className="font-display text-4xl tracking-brutal sm:text-6xl">Bodyweight & photos</h1>
        </div>
        <div className="flex items-center gap-2">
          <SyncBadge status={syncStatus} />
          <button
            onClick={() => setOpen(true)}
            className="inline-flex items-center gap-2 border border-maroon-500/60 bg-maroon-700 px-4 py-3 font-mono text-[11px] uppercase tracking-widest text-bone-50 hover:bg-maroon-600"
          >
            <Plus className="h-3.5 w-3.5" /> Log weight
          </button>
        </div>
      </header>

      <section className="grid gap-4 lg:grid-cols-2">
        {[a, b].map((e, i) => (
          <div key={i} className="border border-bone-50/10 bg-ink-900/60 p-5">
            <div className="flex items-center justify-between">
              <h3 className="font-display text-2xl tracking-brutal">{i === 0 ? "Then" : "Now"}</h3>
              <select
                aria-label={i === 0 ? "Compare from" : "Compare to"}
                value={(i === 0 ? compare.a : compare.b) ?? e?.id ?? ""}
                onChange={(ev) =>
                  setCompare((c) => ({ ...c, [i === 0 ? "a" : "b"]: ev.target.value }))
                }
                className="border border-bone-50/15 bg-ink-900 px-2 py-1 font-mono text-[10px] uppercase tracking-widest"
              >
                {entries.map((x) => (
                  <option key={x.id} value={x.id}>
                    {formatDate(x.date)} · {x.weight}lb
                  </option>
                ))}
              </select>
            </div>
            <div className="mt-4 aspect-[3/4] w-full border border-bone-50/10 bg-gradient-to-br from-ink-800 to-ink-900">
              {e?.photoDataUrl ? (
                <img
                  src={e.photoDataUrl}
                  alt={`Progress on ${e.date}`}
                  className="h-full w-full object-cover"
                />
              ) : (
                <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-bone-300">
                  <ImageIcon className="h-8 w-8" />
                  <span className="font-mono text-[10px] uppercase tracking-widest">
                    No photo for this entry
                  </span>
                </div>
              )}
            </div>
            <div className="mt-4 flex items-baseline justify-between">
              <span className="font-display text-3xl tracking-brutal">{e?.weight ?? "—"}lb</span>
              <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                {e ? formatDate(e.date) : ""}
              </span>
            </div>
            {e?.notes && <p className="mt-2 text-sm italic text-bone-200">{e.notes}</p>}
          </div>
        ))}
      </section>

      <section>
        <h2 className="font-display text-3xl tracking-brutal">All entries</h2>
        <ul className="mt-4 divide-y divide-bone-50/10 border-y border-bone-50/10">
          {entries.map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-4 py-4">
              <span className="inline-flex h-10 w-10 items-center justify-center border border-bone-50/15 bg-ink-900">
                {e.photoDataUrl ? (
                  <img src={e.photoDataUrl} alt="" className="h-full w-full object-cover" />
                ) : (
                  <Scale className="h-4 w-4 text-bone-300" />
                )}
              </span>
              <div className="flex-1">
                <div className="font-display text-xl tracking-brutal">{e.weight}lb</div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                  {formatDate(e.date)}
                </div>
              </div>
              <p className="hidden max-w-md flex-1 text-sm text-bone-200 sm:block">{e.notes}</p>
              <button
                aria-label={`Delete entry from ${formatDate(e.date)}`}
                onClick={() => remove(e.id)}
                className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 text-bone-300 hover:border-maroon-400 hover:text-bone-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      </section>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/80 p-4 backdrop-blur"
            role="dialog"
            aria-modal
            aria-labelledby="log-title"
          >
            <motion.div
              initial={{ y: 20, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              exit={{ y: 20, opacity: 0 }}
              className="w-full max-w-md border border-bone-50/15 bg-ink-900 p-6"
            >
              <h3 id="log-title" className="font-display text-3xl tracking-brutal">
                Log weight
              </h3>
              <div className="mt-5 space-y-4">
                <label className="block">
                  <span className="block font-mono text-[10px] uppercase tracking-widest text-bone-300">
                    Date
                  </span>
                  <input
                    type="date"
                    value={draft.date}
                    onChange={(e) => setDraft({ ...draft, date: e.target.value })}
                    className="mt-1 w-full border border-bone-50/15 bg-ink-950 px-3 py-2 font-mono text-sm"
                  />
                </label>
                <label className="block">
                  <span className="block font-mono text-[10px] uppercase tracking-widest text-bone-300">
                    Weight (lb)
                  </span>
                  <input
                    type="number"
                    inputMode="decimal"
                    step={0.1}
                    value={draft.weight || ""}
                    onChange={(e) => setDraft({ ...draft, weight: parseFloat(e.target.value) || 0 })}
                    className="mt-1 w-full border border-bone-50/15 bg-ink-950 px-3 py-2 font-display text-2xl tracking-brutal"
                  />
                </label>
                <label className="block">
                  <span className="block font-mono text-[10px] uppercase tracking-widest text-bone-300">
                    Photo (optional)
                  </span>
                  <input
                    type="file"
                    accept="image/*"
                    onChange={(e) => e.target.files?.[0] && onPhoto(e.target.files[0])}
                    className="mt-1 block w-full text-sm text-bone-200 file:mr-3 file:border file:border-bone-50/15 file:bg-ink-950 file:px-3 file:py-2 file:font-mono file:text-[10px] file:uppercase file:tracking-widest file:text-bone-100"
                  />
                  {draft.photoDataUrl && (
                    <img
                      src={draft.photoDataUrl}
                      alt="Selected progress"
                      className="mt-3 max-h-40 border border-bone-50/15"
                    />
                  )}
                </label>
                <label className="block">
                  <span className="block font-mono text-[10px] uppercase tracking-widest text-bone-300">
                    Notes
                  </span>
                  <textarea
                    rows={3}
                    value={draft.notes ?? ""}
                    onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                    className="mt-1 w-full border border-bone-50/15 bg-ink-950 px-3 py-2 text-sm"
                  />
                </label>
              </div>
              <div className="mt-6 flex justify-end gap-2">
                <button
                  onClick={() => setOpen(false)}
                  className="border border-bone-50/15 bg-ink-950 px-4 py-2 font-mono text-[10px] uppercase tracking-widest"
                >
                  Cancel
                </button>
                <button
                  onClick={save}
                  className="border border-maroon-500/60 bg-maroon-700 px-4 py-2 font-mono text-[10px] uppercase tracking-widest hover:bg-maroon-600"
                >
                  Save
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function offset(days: number) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}
