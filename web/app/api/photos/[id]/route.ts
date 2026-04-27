import { NextResponse } from "next/server";
import { deletePhoto, readPhoto } from "@/lib/db";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(_req: Request, { params }: { params: { id: string } }) {
  const photo = readPhoto(params.id);
  if (!photo) return NextResponse.json({ error: "not found" }, { status: 404 });
  return new NextResponse(new Uint8Array(photo.bytes), {
    status: 200,
    headers: {
      "content-type": photo.mime,
      "cache-control": "private, max-age=31536000, immutable",
    },
  });
}

export async function DELETE(_req: Request, { params }: { params: { id: string } }) {
  deletePhoto(params.id);
  return NextResponse.json({ ok: true });
}
