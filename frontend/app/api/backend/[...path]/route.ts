import type { NextRequest } from "next/server";

// Forwards /api/backend/* to the FastAPI service. Resolved at request time, so the
// same build works locally (localhost:8000) and in Docker (http://api:8000).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

async function proxy(request: NextRequest, ctx: RouteContext<"/api/backend/[...path]">) {
  const { path } = await ctx.params;
  const target = `${BACKEND_URL}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers: { "content-type": request.headers.get("content-type") ?? "application/json" },
      body: request.method === "GET" ? undefined : await request.text(),
      cache: "no-store",
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: `Backend unreachable at ${BACKEND_URL}` }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST };
