import { createHmac, timingSafeEqual, randomUUID } from 'node:crypto';

export function signSession(secret, now = Date.now()) {
  const payload = Buffer.from(JSON.stringify({ id: randomUUID(), exp: now + 120_000 })).toString('base64url');
  return `${payload}.${createHmac('sha256', secret).update(payload).digest('base64url')}`;
}

export function verifySession(token, secret, now = Date.now()) {
  if (typeof token !== 'string' || token.length > 500) return null;
  const [payload, signature, extra] = token.split('.');
  if (!payload || !signature || extra) return null;
  const expected = createHmac('sha256', secret).update(payload).digest();
  const actual = Buffer.from(signature, 'base64url');
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, 'base64url').toString());
    return typeof session.id === 'string' && session.exp > now && session.exp <= now + 120_000 ? session : null;
  } catch { return null; }
}

export function validateMessages(messages) {
  return Array.isArray(messages) && messages.length > 0 && messages.length <= 20 &&
    messages.at(-1)?.role === 'user' && messages.every((m) =>
      m && ['user', 'assistant'].includes(m.role) && typeof m.content === 'string' &&
      m.content.trim().length > 0 && m.content.length <= 1500);
}

// Atomic fixed-window counters shared by all serverless instances.
const memory = new Map();
export async function consume(key, limit, seconds) {
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (url && token) {
    const script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n";
    const response = await fetch(url, {
      method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(['EVAL', script, '1', `voice-demo:${key}`, String(seconds)]),
      signal: AbortSignal.timeout(5000), cache: 'no-store',
    });
    if (!response.ok) throw new Error('Quota service unavailable');
    const data = await response.json();
    if (data.error || typeof data.result !== 'number') throw new Error('Quota service unavailable');
    return data.result <= limit;
  }
  if (process.env.VERCEL || process.env.NODE_ENV === 'production') throw new Error('Shared quota storage is required');
  const now = Date.now();
  for (const [k, v] of memory) if (v.expires <= now) memory.delete(k);
  const entry = memory.get(key) || { count: 0, expires: now + seconds * 1000 };
  entry.count++;
  memory.set(key, entry);
  return entry.count <= limit;
}

export function visitorKey(request, secret) {
  // Vercel overwrites x-vercel-forwarded-for. Do not trust client-supplied generic forwarding headers.
  const ip = process.env.VERCEL ? request.headers.get('x-vercel-forwarded-for') || 'unknown' : 'local';
  return createHmac('sha256', secret).update(ip).digest('hex').slice(0, 32);
}

export function sameOrigin(request) {
  return request.headers.get('origin') === new URL(request.url).origin;
}
