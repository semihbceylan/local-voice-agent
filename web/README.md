# Public browser voice demo

## Turkish browser voice

The website and speech recognition use Turkish (`tr-TR`). Chat uses the configured OpenRouter free model with a Turkish system prompt. EMA Lightning runs on the visitor's device using WebGPU or WASM, in a worker, on both localhost and Vercel. No Python voice server is required.

The browser downloads pinned ONNX weights (~36 MB plus runtime) from Hugging Face and ONNX Runtime 1.30.0 from jsDelivr. Models are cached when possible. iPhone/iPad default to WASM due to known WebGPU runtime stability issues. Device performance and browser support vary. Audio chunks play as they are generated; interruption stops playback and discards obsolete inference results.

Browser engine adapted from ozcancelik/ema-lightning-web at a9a688b19badc36486cdc2444ba957bc63ac860e; model conversion pinned to 13c431db0356b2f7fafb1247cd823ec0d777c820. See `public/ema/LICENSE`, `NOTICE`, and `licenses/` for attribution. The optional legacy Python bridge is local-only and is not used by the website.

General-purpose AI voice chat with a Next.js frontend + server-only OpenRouter streaming endpoint. No paid speech APIs, audio transport service, local Python model, or login is required. This is independent of the existing local CLI and its business workflows.

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

1. Import this repository, select branch `public-voice-demo`, and set **Root Directory** to `web` (Next.js preset).
2. Set the variables from `.env.example` in Vercel project settings. `SESSION_SECRET` should be a random 32-byte secret (`openssl rand -hex 32`).
3. Create a free Upstash Redis database and set its REST URL/token. Production fails closed without shared quotas; process memory is used only for local development. Do not enable paid upgrades.
4. Deploy. For previews, configure the same environment variables for the Preview environment. A feature branch creates a preview deployment; set it as the production branch only if that is intended.
5. Test the microphone and speaker on the HTTPS deployment using desktop Chrome or Edge. Check free-tier limits/terms for Vercel, Upstash, and OpenRouter before publishing. Use provider usage controls; zero cost is not a promise of unlimited usage.

## Behavior and limits

- One start click for permissions, then automatic turn-taking for up to two minutes.
- Browser recognition pauses during responses to avoid echo. **Automatic spoken barge-in is not supported**; the interrupt button cancels an answer and resumes listening. This is a browser voice demo, not full-duplex WebRTC.
- Recognition support varies; desktop Chrome/Edge are recommended. Some browsers use vendor servers for recognition. EMA uses one fixed Turkish voice; no paid TTS service is used.
- Audio is not uploaded to this app. Transcripts go to OpenRouter; review its/model-provider policies before sending sensitive information. The app does not persist transcripts, but providers have their own logging policies.
- Server-enforced: three starts per IP/hour, signed two-minute sessions, 12 turns/session, configurable global daily requests (100 default, 1000 hard maximum), bounded inputs and outputs. IP quotas are not identity or strong bot protection; origin checks do not stop scripted clients.
- Local development optionally reads `web/demo-limits.local.json` (ignored by Git) with positive integer fields `startsPerHour`, `turnsPerSession`, and `requestsPerDay`. It is re-read on requests; production never uses it. Without the file, normal limits apply. Voice requests disable optional model reasoning and allow 512 output tokens; models that cannot disable reasoning may still exhaust that budget.
- There is no guaranteed global concurrency limit. Global request quotas and provider limits constrain exposure. Request cancellation is best effort and does not guarantee upstream quota recovery.
- General conversation only; no tools, live web access, or access to private data. The agent must disclose its limitations.

```sh
npm test
npm run build
```
