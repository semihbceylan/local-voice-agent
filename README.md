# Sadece Sohbet — Turkish Voice Chat

A Turkish voice chat website with OpenRouter for conversation and EMA Lightning
running directly on the visitor’s device for speech generation. Start a session,
allow microphone access, and speak naturally—no account or push-to-talk required.

**Website:** [just-talk-voice.vercel.app](https://just-talk-voice.vercel.app)

> **Proprietary software, all rights reserved.** See [LICENSE](LICENSE).
> Bundled third-party components retain their own licenses and notices.

## How it works

```text
Microphone → Turkish browser speech recognition → OpenRouter
                                                     ↓
Speakers ← browser-side EMA Lightning ← Turkish reply text
```

| Component | Implementation |
|---|---|
| Website | Next.js and React, deployed on Vercel |
| Speech recognition | Browser Speech Recognition API, configured for `tr-TR` |
| Conversation | Server-side OpenRouter connection to a configurable free model |
| Speech generation | EMA Lightning ONNX, running in a browser worker |
| Acceleration | WebGPU where supported; WASM CPU fallback |
| Production quotas | Shared Redis counters and signed, time-limited sessions |

## Features and limitations

- Turkish interface and conversational instructions.
- Automatic turn-taking after the initial start click.
- Streaming text replies and chunked audio playback.
- On-device speech generation without a paid TTS API or Python voice server.
- Model caching after the initial download.
- An interrupt button that stops playback and resumes listening.
- Server-only API keys, bounded requests, and configurable usage limits.

Recognition pauses during replies to avoid echo, so automatic spoken interruption
is not supported. Browser recognition support varies; desktop Chrome or Edge is
recommended. EMA uses one fixed Turkish voice. Performance depends on the visitor’s
device. iPhone and iPad default to WASM.

## Run locally

Use Node.js 22 LTS or later:

```sh
git clone https://github.com/semihbceylan/local-voice-agent.git
cd local-voice-agent
git switch public-voice-demo
cd web
npm ci
cp .env.example .env.local
```

Set these values in `web/.env.local`:

```dotenv
OPENROUTER_API_KEY=your-key
OPENROUTER_MODEL=your-selected-model-id:free
SESSION_SECRET=your-random-secret
```

Generate a session secret with `openssl rand -hex 32`. Never commit credentials
or prefix them with `NEXT_PUBLIC_`.

```sh
npm run dev
```

Open **http://localhost:3000**. On first start, the browser downloads approximately
36 MB of model files plus runtime files. Local development uses in-memory quotas
and does not require Redis. Optional local overrides in
`web/demo-limits.local.json` are ignored by Git and never used in production.

## Deploy to Vercel

Use the `public-voice-demo` branch, the Next.js preset, and **Root Directory: `web`**.
Configure the server-only environment variables from
[`web/.env.example`](web/.env.example), including the shared Redis REST credentials
required for production quotas, then deploy.

EMA runs on visitors’ devices on Vercel too; no separately hosted speech service
is required. Free OpenRouter models and hosting tiers have quotas and availability
limits. No paid LLM fallback is configured.

See [`web/README.md`](web/README.md) for deployment details and usage limits.

## Privacy

- Conversation text is sent through the server to OpenRouter and its model provider.
- Speech recognition may send microphone audio to the browser vendor’s service.
- EMA synthesizes replies locally in the visitor’s browser.
- Model files download from Hugging Face; the inference runtime loads from jsDelivr.
- The website does not persist conversation transcripts. Providers have their own
  retention policies; avoid sharing sensitive information.

## Development

```sh
cd web
npm test
npm run build
```

The repository also retains an earlier Python/macOS voice client and its tests.
Those files are independent of the website and are not required to run or deploy it.

## Credits

- [EMA Lightning](https://huggingface.co/canberkkkkkk/ema-lightning) by Canberk Aslan.
- [Browser engine](https://github.com/ozcancelik/ema-lightning-web) and
  [ONNX conversion](https://huggingface.co/ozcancelik/ema-lightning-onnx) by ozcancelik.
- ONNX Runtime Web and normalizer-tr.

Pinned versions and third-party license notices are documented in
[`web/README.md`](web/README.md) and [`web/public/ema/NOTICE`](web/public/ema/NOTICE).
