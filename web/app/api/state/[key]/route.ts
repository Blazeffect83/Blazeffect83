import { NextResponse } from "next/server";
import { readState, writeState } from "@/lib/db";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const ALLOWED = new Set(["workout-draft", "meals", "progress-entries"]);

function guard(key: string) {
  if (!ALLOWED.has(key)) return NextResponse.json({ error: "unknown key" }, { status: 400 });
  return null;
}

export async function GET(_req: Request, { params }: { params: { key: string } }) {
  const denied = guard(params.key);
  if (denied) return denied;
  const value = readState<unknown>(params.key, null);
  return NextResponse.json({ key: params.key, value });
}

export async function PUT(req: Request, { params }: { params: { key: string } }) {
  const denied = guard(params.key);
  if (denied) return denied;
  let value: unknown;
  try {
    value = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }
  writeState(params.key, value);
  return NextResponse.json({ ok: true });
}
