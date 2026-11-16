import { consume, sameOrigin, verifySession } from '../../../lib/security.mjs';
import { localVoice } from '../../../lib/voice-config.mjs';

export const runtime = 'nodejs';
export async function POST(request) {
  if (!localVoice()) return Response.json({ error: 'EMA yalnızca yerel geliştirmede kullanılabilir.' }, { status: 503 });
  if (!sameOrigin(request)) return Response.json({ error: 'İstek reddedildi.' }, { status: 403 });
  const secret = process.env.SESSION_SECRET;
  const session = secret && verifySession(request.headers.get('authorization')?.replace(/^Bearer /, ''), secret);
  if (!session) return Response.json({ error: 'Oturum sona erdi.' }, { status: 401 });
  try {
    const raw = await request.text();
    if (raw.length > 6000) throw new Error();
    const { text } = JSON.parse(raw);
    if (typeof text !== 'string' || !text.trim() || text.length > 1500) throw new Error();
    if (!await consume(`speech:${session.id}`, 60, 180)) return Response.json({ error: 'Ses sınırına ulaşıldı.' }, { status: 429 });
    const response = await fetch('http://127.0.0.1:8765/speak', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(180_000)]),
    });
    if (!response.ok) throw new Error();
    return new Response(response.body, { headers: { 'Content-Type': 'audio/wav', 'Cache-Control': 'no-store' } });
  } catch {
    return Response.json({ error: 'EMA sesi üretilemedi. Yerel EMA servisinin çalıştığını kontrol edin.' }, { status: 503 });
  }
}
