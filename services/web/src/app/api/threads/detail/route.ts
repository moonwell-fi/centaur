/** /api/threads/detail?key=... — proxy to backend API */

import { apiGet } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const key = searchParams.get("key") || "";
  if (!key) {
    return Response.json({ error: "Missing thread key" }, { status: 400 });
  }

  try {
    const res = await apiGet("/agent/threads/detail", { key });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      return Response.json(
        { error: text || `API error (${res.status})` },
        { status: res.status, headers: { "Cache-Control": "no-store" } },
      );
    }
    const data = await res.json();
    return Response.json(data, {
      headers: { "Cache-Control": "public, s-maxage=5, stale-while-revalidate=3" },
    });
  } catch (err) {
    return Response.json(
      { error: err instanceof Error ? err.message : "API error" },
      { status: 500, headers: { "Cache-Control": "no-store" } },
    );
  }
}
