# DemoShop Local Voice Agent

A fully local voice customer service agent for the macOS. Nothing leaves the
machine: the microphone, the transcription, the language model and the voice
are all local.

> **Proprietary software, all rights reserved.** This repository is published
> for viewing only. See [LICENSE](LICENSE).

```
you speak  ->  ffmpeg + WebRTC VAD  ->  whisper.cpp  ->  n8n (Ollama)  ->  Kokoro  ->  you hear
```

| Part | What it is |
|---|---|
| Capture | one long lived `ffmpeg` stream, resumable WebRTC VAD endpointer |
| Transcription | `whisper.cpp` with `ggml-base.en` |
| Routing | n8n workflow: deterministic Data Table lookup for orders, Ollama for everything else |
| Voice | `mlx-audio` Kokoro, the pipeline is loaded once and reused |

## What it does

* **Order questions are answered from the database, not from the model.**
  "Where is my order 1001?" is a filtered lookup in the `customer_orders`
  Data Table; the language model never sees it.
* **Spoken order numbers are understood the way people say them**:
  `1,001`, `one thousand and one` and even `1,000 and 1` all resolve to 1001.
* **Uncertain input is never guessed.** Two possible order numbers, or a
  number that cannot be interpreted safely, produce a question instead of a
  lookup. A single misheard digit produces
  *"I couldn't find order #1011. Did you mean order #1001?"*
* **Call mode:** the microphone stays open, the agent answers by itself and
  hangs up when you say goodbye.

## Quick start

```bash
git clone <this repository>
cd <repo-name>
./scripts/setup.sh          # virtualenv, dependencies, Whisper model
cp .env.example .env        # add your n8n API key
.venv/bin/python voice_agent.py
```

The client talks to n8n, so the workflow has to be deployed first:

```bash
export N8N_API_KEY=...      # never put the key in a file
./scripts/deploy_workflow.sh
```

See [docs/n8n-setup.md](docs/n8n-setup.md) for the Data Table, the Ollama
credential and the webhook.

## Using the client

```bash
.venv/bin/python voice_agent.py                    # call mode: always listening
.venv/bin/python voice_agent.py --mode arm         # press Enter to talk
.venv/bin/python voice_agent.py --mode ptt         # fixed length recording
.venv/bin/python voice_agent.py --barge-in         # interrupt the answer
.venv/bin/python voice_agent.py --no-play          # print only, no voice
.venv/bin/python voice_agent.py --list-output-devices
.venv/bin/python voice_agent.py --output-device "Mac mini Hoparlörü"
```

## Repository layout

```
voice_agent.py             client: capture, transcription, playback, call loop
vad_stream.py              resumable voice activity detection
farewell.py                goodbye detection
build_voice_workflow.py    generates the n8n workflow
workflow/
  extract_order_id.js      the order parser (embedded into the workflow)
workflows/
  voice_agent_production.json   the generated n8n workflow
scripts/
  setup.sh                 environment, dependencies, Whisper model
  deploy_workflow.sh       build and deploy the workflow to n8n
  test_all.sh              every test suite
tests/                     unit, integration and live microphone tests
docs/                      architecture, n8n setup, troubleshooting
```

## Tests

```bash
./scripts/test_all.sh
```

| Suite | What it covers |
|---|---|
| `tests/parser/` | the order parser, run with `node --test` against the file that is embedded into the workflow |
| `tests/test_vad.py` | the endpointer: threshold, noise floor, pauses, start clipping, breakdown |
| `tests/test_call_mode.py` | the continuous call loop, turn after turn, hanging up on a goodbye |
| `tests/test_farewell.py` | goodbye detection and the phrases that must not trigger it |
| `tests/stt_reliability.py` | synthesised speech -> whisper.cpp -> parser |
| `tests/test_integration.py` | end to end over HTTP against freshly created isolated n8n workflows |
| `tests/live_mic_test.py` | the physical microphone test, prints the timing of every stage |

The suites that need a microphone or an n8n instance are skipped when they
are not available. No recording of a real microphone is part of this
repository; the speech fixture is generated locally with Kokoro.

## Configuration

Everything is configured through the environment or command line flags, for
example `VAD_SILENCE_MS`, `VAD_MAX_SECONDS`, `AUDIO_DEVICE`,
`WHISPER_MODEL`, `KOKORO_VOICE`, `OUTPUT_DEVICE`, `N8N_WEBHOOK_URL`. The full
list is in [docs/architecture.md](docs/architecture.md) and in the client
itself (`voice_agent.py --help`).

## Documentation

* [docs/architecture.md](docs/architecture.md) - routing, parser, endpointer, latency
* [docs/n8n-setup.md](docs/n8n-setup.md) - n8n, Data Table, credential, deployment
* [docs/troubleshooting.md](docs/troubleshooting.md) - the known failure modes and their fixes

## Privacy

Nothing is sent to a cloud service. The only outbound connection is to your
own n8n instance on localhost. The repository contains no API key, no
microphone recording and no personal data: `.env`, `*.wav`, `logs/` and
`backups/` are ignored by git.
