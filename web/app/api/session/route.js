import { consume, sameOrigin, signSession, visitorKey } from '../../../lib/security.mjs';
import { demoLimits } from '../../../lib/limits.mjs';

export const runtime = 'nodejs';
export async function POST(request) {
  if (!sameOrigin(request)) return Response.json({ error: 'İstek reddedildi.' }, { status: 403 });
  const secret = process.env.SESSION_SECRET;
  if (!secret || !process.env.OPENROUTER_API_KEY || !process.env.OPENROUTER_MODEL?.endsWith(':free')) {
    return Response.json({ error: 'Sohbet ayarları henüz tamamlanmadı. Lütfen daha sonra deneyin.' }, { status: 503 });
  }
  try {
    const limits = await demoLimits();
    if (!await consume(`starts:${visitorKey(request, secret)}`, limits.startsPerHour, 3600)) {
      return Response.json({ error: 'Deneme sınırına ulaştınız. Lütfen bir saat sonra tekrar deneyin.' }, { status: 429 });
    }
    return Response.json({ token: signSession(secret), duration: 120, speech: 'ema-browser' }, { headers: { 'Cache-Control': 'no-store' } });
  } catch {
    return Response.json({ error: 'Sohbet geçici olarak kullanılamıyor.' }, { status: 503 });
  }
}
