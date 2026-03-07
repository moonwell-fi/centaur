/** GET /api/portfolio/funds -> POST /tools/paradigmdb/db_query (list fund names) */

import { resilientFetch, API_URL, ApiError } from "@/lib/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: Request) {
  try {
    const res = await resilientFetch(`${API_URL}/tools/paradigmdb/db_query`, {
      method: "POST",
      body: JSON.stringify({
        query: 'SELECT DISTINCT name FROM "Fund" ORDER BY name',
        limit: 50,
      }),
      signal: request.signal,
      timeoutMs: 10_000,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(`Funds API error (${res.status}): ${text.slice(0, 300)}`, res.status, res.status >= 500);
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
