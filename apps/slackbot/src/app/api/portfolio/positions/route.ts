/** GET /api/portfolio/positions -> POST /tools/paradigmdb/db_positions */

import { resilientFetch, API_URL, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const fund = searchParams.get("fund") || undefined;
  const limit = parseInt(searchParams.get("limit") || "200", 10);

  try {
    const body: Record<string, unknown> = { limit };
    if (fund) body.fund = fund;

    const res = await resilientFetch(`${API_URL}/tools/paradigmdb/db_positions`, {
      method: "POST",
      body: JSON.stringify(body),
      signal: request.signal,
      timeoutMs: 15_000,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(`Positions API error (${res.status}): ${text.slice(0, 300)}`, res.status, res.status >= 500);
    }

    const data = await res.json();
    return Response.json(data, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const status = err instanceof ApiError ? (err.status ?? 502) : 502;
    return Response.json(
      { error: err instanceof Error ? err.message : "API unreachable" },
      { status, headers: { "Cache-Control": "no-store" } },
    );
  }
}
