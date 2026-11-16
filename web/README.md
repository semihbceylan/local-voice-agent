# Public browser voice demo

Next.js frontend + server-only OpenRouter streaming endpoint. No paid speech APIs, audio transport service, local Python model, or login is required. This is separate from the existing local CLI and does not connect to its n8n orders database.

## Local development

Use Node 22 LTS or later:

```sh
cd web
npm ci
cp .env.example .env.local
# Fill OPENROUTER_API_KEY, OPENROUTER_MODEL and SESSION_SECRET.
npm run dev
```

Pick an available OpenRouter text model whose ID ends with `:free`. No paid fallback is configured. Free availability, quotas, and latency are not guaranteed. Use a dedicated key with a small/zero credit allowance as supported by your account. Never prefix secrets with `NEXT_PUBLIC_`.

## Vercel

1. Import this repository, select branch `feat/public-voice-demo`, and set **Root Directory** to `web` (Next.js preset).
2. Set the variables from `.env.example` in Vercel project settings. `SESSION_SECRET` should be a random 32-byte secret (`openssl rand -hex 32`).
3. Create a free Upstash Redis database and set its REST URL/token. Production fails closed without shared quotas; process memory is used only for local development. Do not enable paid upgrades.
4. Deploy. For previews, configure the same environment variables for the Preview environment. A feature branch creates a preview deployment; set it as the production branch only if that is intended.
5. Test the microphone and speaker on the HTTPS deployment using desktop Chrome or Edge. Check free-tier limits/terms for Vercel, Upstash, and OpenRouter before publishing. Use provider usage controls; zero cost is not a promise of unlimited usage.

## Behavior and limits

- One start click for permissions, then automatic turn-taking for up to two minutes.
- Browser recognition pauses during responses to avoid echo. **Automatic spoken barge-in is not supported**; the interrupt button cancels an answer and resumes listening. This is a browser voice demo, not full-duplex WebRTC.
- Recognition support varies; desktop Chrome/Edge are recommended. Some browsers use vendor servers for recognition. TTS voices vary by device. The browser may require its own speech service/network connection.
- Audio is not uploaded to this app. Transcripts go to OpenRouter; review its/model-provider policies before sending sensitive information. The app does not persist transcripts, but providers have their own logging policies.
- Server-enforced: three starts per IP/hour, signed two-minute sessions, 12 turns/session, configurable global daily requests (100 default, 1000 hard maximum), bounded inputs and outputs. IP quotas are not identity or strong bot protection; origin checks do not stop scripted clients.
- There is no guaranteed global concurrency limit. Global request quotas and provider limits constrain exposure. Request cancellation is best effort and does not guarantee upstream quota recovery.
- No real order lookup, account tools, purchases, or real shop policies. The agent must disclose its limitations.

```sh
npm test
npm run build
```
