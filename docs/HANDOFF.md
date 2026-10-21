# Handoff

Status of the repository and the facts that are expensive to rediscover.
Read this before changing anything.

## Where we are

| | |
|---|---|
| Repository | `https://github.com/semihbceylan/local-voice-agent` (public, proprietary licence) |
| Local folder | `~/local-voice-agent` (git repo, branch `main`) |
| n8n workflow | `nPLoCe6uuit1oMqs` "DemoShop Voice Agent API", webhook `POST /webhook/voice-agent` |
| Data | n8n Data Table `customer_orders` (id `RavtVTN9w9NPZFuZ`), three fictional orders |
| Language model | Ollama `qwen3:4b-instruct` (host `http://host.docker.internal:11434` from the container) |
| Speech | whisper.cpp `ggml-base.en`, Kokoro `mlx-community/Kokoro-82M-bf16`, voice `af_heart` |
| CI | GitHub Actions, green: parser, VAD, call mode, farewell, workflow build guard, recognition |

Everything is local. No cloud service, no paid API.

## Architecture

```
microphone -> ffmpeg (one long lived process) -> 30 ms frames
           -> VadStream (resumable endpointer) -> whisper.cpp
           -> n8n: Data Table lookup for orders, Ollama for everything else
           -> Kokoro (loaded once) -> afplay / sounddevice
```

| File | Role |
|---|---|
| `voice_agent.py` | client: capture, transcription, TTS, call mode, latency log |
| `vad_stream.py` | the endpointer as a resumable state machine |
| `farewell.py` | goodbye detection (ends the call) |
| `build_voice_workflow.py` | **generates** the n8n workflow, it is the source of truth |
| `workflow/extract_order_id.js` | the order parser, embedded into the workflow verbatim |
| `workflows/voice_agent_production.json` | the generated workflow, committed for review and import |
| `scripts/setup.sh` | venv, dependencies, Whisper model, tool check |
| `scripts/deploy_workflow.sh` | build and deploy the workflow to n8n |
| `scripts/test_all.sh` | every test suite |
| `tests/` | parser, VAD, call mode, farewell, build guard, recognition, integration, live mic |

## Facts that cost time to find

**n8n 2.44.4**
* The string operator `isNotEmpty` **always evaluates to false**. Use
  `notEquals` with an empty value instead. (The old workflow used it and
  silently routed everything to the false branch.)
* A Code node in `runOnceForAllItems` mode must return `[{ json: {...} }]`.
* There is no `$httpRequest` in a Code node. Query the Data Table with the
  native Data Table node.
* A workflow update through the API re-registers the webhook path a moment
  later; right after the update the webhook can answer 404 for a second.
* `PUT /workflows/{id}` does not change the active state, but the webhook
  path has to be re-registered, so the deploy script verifies the webhook
  after every deployment.

**WebRTC VAD**
* It has a **hangover of 5.7 to 7.4 seconds**: after real speech it
  reports room tone as speech for several seconds. Its state is therefore
  reset regularly (`VAD_RESET_FRAMES`) and the end of speech is decided by
  the **level**, not by the VAD.
* A steady noise floor above roughly -30 dBFS is reported as speech. That
  is why the endpointer combines the VAD with an energy gate.
* A fresh VAD instance flags everything as speech for its first frames, so
  the instance has to be used continuously, not per frame.

**Kokoro / mlx-audio**
* The phonemiser `misaki[en]` is **not** a dependency of `mlx-audio[tts]`
  and has to be declared, otherwise the text to speech engine cannot turn
  text into audio and every test that generates speech fails.
* Emoji cannot be spoken. The workflow strips them before the answer
  reaches Kokoro, otherwise the phonemiser warns and reads them as noise.

**The repository**
* The workflow JSON is a **build artifact**. `build_voice_workflow.py` is
  the source of truth, `tests/test_workflow_build.py` guards that the
  committed file matches the builder and contains no secret.
* Node and webhook ids are derived from the node name, so a rebuild
  produces the same file and the committed copy never drifts.
* The speech fixture is **generated** with Kokoro at test time
  (`tests/fixtures_gen.py`). No recording of a real microphone is in the
  repository.
* `*.wav`, `logs/`, `backups/`, `.env` and the 141 MB Whisper model are
  git-ignored.

## The live microphone (measured)

Bluetooth headset `JBL VIBE BEAM`, AVFoundation device `:0`.

| Measurement | Value |
|---|---|
| device open | 0.19 s (once per call, the device stays open in call mode) |
| room tone | around -63 dBFS, the noise floor is digital silence |
| speech | -9 to -20 dBFS |
| end of speech | threshold + one frame, 810 ms with an 800 ms threshold |
| ffmpeg shutdown | 0.002 s (it used to be 3 s, SIGTERM is not handled while ffmpeg blocks on a full pipe) |
| whisper.cpp | 0.25 - 0.55 s |
| n8n order lookup | 0.02 - 0.08 s |
| n8n general question | 0.8 - 1.5 s |
| Kokoro, warm | 0.3 - 0.6 s |
| time to first audio after the customer stops | about 1.0 s |

## Running it

```bash
cd ~/local-voice-agent
./scripts/setup.sh                       # once
cp .env.example .env                     # add the n8n API key
./scripts/test_all.sh                    # every suite
.venv/bin/python voice_agent.py          # call mode
.venv/bin/python voice_agent.py --help   # all the knobs
```

Deploy a workflow change:

```bash
export N8N_API_KEY=...
export N8N_WORKFLOW_ID=nPLoCe6uuit1oMqs
./scripts/deploy_workflow.sh
```

Live microphone check:

```bash
.venv/bin/python tests/live_mic_test.py --device :0 --silence-ms 800
VAD_DEBUG=1 .venv/bin/python tests/live_mic_test.py --device :0   # frame by frame
```

## Notes for the next change

* n8n runs in Docker. If the API refuses the connection, Docker Desktop is
  not running: `open -a Docker` and wait for the daemon, then check
  `docker ps` and that the n8n container is up.
* Never put the API key in a file, a workflow or a script.
* Keep the proprietary licence, the local-only design and the published
  privacy promise: no key, no microphone recording, no personal data.
* Run `./scripts/test_all.sh` before pushing, and let the CI do the same.
