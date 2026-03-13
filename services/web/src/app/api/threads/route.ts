/** GET /api/threads — proxy to backend API */

import { apiGet } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET() {
  try {
    const res = await apiGet("/agent/threads");
    const data = await res.json();
    return Response.json(data, {
      headers: { "Cache-Control": "public, s-maxage=3, stale-while-revalidate=1" },
    });
  } catch (err) {
    console.error("Failed to list threads:", err);
    return Response.json(
      { error: err instanceof Error ? err.message : "API error" },
      { status: 500, headers: { "Cache-Control": "no-store" } },
    );
  }
}
