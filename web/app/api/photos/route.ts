import { NextResponse } from "next/server";
import { savePhoto } from "@/lib/db";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const MAX_BYTES = 8 * 1024 * 1024; // 8MB

export async function POST(req: Request) {
  const form = await req.formData().catch(() => null);
  if (!form) return NextResponse.json({ error: "expected multipart/form-data" }, { status: 400 });
  const file = form.get("file");
  if (!(file instanceof File)) return NextResponse.json({ error: "missing file" }, { status: 400 });
  if (file.size > MAX_BYTES) return NextResponse.json({ error: "too large" }, { status: 413 });
  if (!file.type.startsWith("image/"))
    return NextResponse.json({ error: "unsupported type" }, { status: 415 });

  const buf = Buffer.from(await file.arrayBuffer());
  const id = `p_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  savePhoto(id, file.type, buf);
  return NextResponse.json({ id, url: `/api/photos/${id}` });
}
