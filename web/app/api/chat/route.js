import { consume, sameOrigin, validateMessages, verifySession } from '../../../lib/security.mjs';

export const runtime = 'nodejs';
export const maxDuration = 60;

export async function POST(request) {
  if (!sameOrigin(request)) return Response.json({ error: 'Request not allowed.' }, { status: 403 });
  const secret = process.env.SESSION_SECRET;
  const session = secret && verifySession(request.headers.get('authorization')?.replace(/^Bearer /, ''), secret);
  if (!session) return Response.json({ error: 'Your demo session has ended.' }, { status: 401 });
  const model = process.env.OPENROUTER_MODEL;
  if (!model?.endsWith(':free') || !process.env.OPENROUTER_API_KEY) {
    return Response.json({ error: 'A free model has not been configured.' }, { status: 503 });
  }
  let messages;
  try {
    // Bound the body before parsing it, including requests without Content-Length.
    const reader = request.body.getReader();
    let size = 0;
    const chunks = [];
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 20_000) { await reader.cancel(); return Response.json({ error: 'Message too large.' }, { status: 413 }); }
      chunks.push(Buffer.from(value));
    }
    ({ messages } = JSON.parse(Buffer.concat(chunks).toString()));
    if (!validateMessages(messages)) throw new Error();
  } catch { return Response.json({ error: 'Invalid conversation.' }, { status: 400 }); }
  try {
    const daily = Math.min(1000, Math.max(1, Number(process.env.DEMO_DAILY_REQUEST_LIMIT) || 100));
    if (!await consume(`turns:${session.id}`, 12, 180) ||
        !await consume(`day:${new Date().toISOString().slice(0, 10)}`, daily, 86400)) {
      return Response.json({ error: 'The demo allowance has been reached. Please try again later.' }, { status: 429 });
    }
    const upstream = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model, stream: true, max_tokens: 180,
        messages: [{ role: 'system', content: 'You are the friendly DemoShop voice concierge in a public demonstration. Reply conversationally in one to three short sentences, without markdown. You can explain shopping, delivery, and returns in general. You have no access to orders, accounts, tools, or live inventory. Never claim to look up an order or invent shop policies. Do not request personal, payment, or account information. Explain that this is a demo when relevant.' }, ...messages],
      }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(45_000)]),
    });
    if (!upstream.ok || !upstream.body) {
      return Response.json({ error: 'The free model is busy or unavailable. Please try again shortly.' }, { status: 503 });
    }
    return new Response(upstream.body, { headers: {
      'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
    } });
  } catch {
    return Response.json({ error: 'The voice service is temporarily unavailable.' }, { status: 503 });
  }
}
