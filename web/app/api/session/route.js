import { consume, sameOrigin, signSession, visitorKey } from '../../../lib/security.mjs';

export const runtime = 'nodejs';
export async function POST(request) {
  if (!sameOrigin(request)) return Response.json({ error: 'Request not allowed.' }, { status: 403 });
  const secret = process.env.SESSION_SECRET;
  if (!secret || !process.env.OPENROUTER_API_KEY || !process.env.OPENROUTER_MODEL?.endsWith(':free')) {
    return Response.json({ error: 'The demo is not configured yet. Please try again later.' }, { status: 503 });
  }
  try {
    if (!await consume(`starts:${visitorKey(request, secret)}`, 3, 3600)) {
      return Response.json({ error: 'You have reached the demo limit. Please try again in an hour.' }, { status: 429 });
    }
    return Response.json({ token: signSession(secret), duration: 120 }, { headers: { 'Cache-Control': 'no-store' } });
  } catch {
    return Response.json({ error: 'The demo is temporarily unavailable.' }, { status: 503 });
  }
}
