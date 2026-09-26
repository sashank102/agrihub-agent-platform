const API_URL =
  process.env.API_INTERNAL_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://127.0.0.1:8000";

export async function GET() {
  try {
    const response = await fetch(`${API_URL}/ready`, { cache: "no-store" });
    if (!response.ok) {
      return Response.json({ status: "not-ready" }, { status: 503 });
    }
    return Response.json({ status: "ready" });
  } catch {
    return Response.json({ status: "not-ready" }, { status: 503 });
  }
}
