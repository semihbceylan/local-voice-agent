# Architecture

```
microphone
  -> ffmpeg (avfoundation, one long lived process)
  -> 30 ms PCM frames
  -> VadStream            resumable endpointer
  -> whisper.cpp          transcription
  -> n8n webhook          routing and answering
  -> Kokoro (mlx-audio)   speech
  -> afplay / sounddevice playback
```

## The n8n routing

```
POST /webhook/voice-agent
        |
  Normalize Input          trim, validate, session id
        |
  Extract Order ID         the parser (workflow/extract_order_id.js)
        |
  Has Order ID?            route == "order"
     /            \
   YES              NO
    |                |
  Lookup Order   Needs Clarification?
  (Data Table)      /          \
    |              YES          NO
  Order Found?      |            |
   /      \         |        AI Agent (Ollama + memory)
 FOUND   MISS    Format      |
  |        |    Clarification  |
  |     List Known Orders      |
  |        |            Normalize AI Response
  |     Suggest Similar       |
  |        |                  |
  +--------+------------------+
           |
     Respond to Webhook
     { "response": "...", "sessionId": "..." }
```

* **Order questions never reach the language model.** They are answered from
  the `customer_orders` Data Table with a filtered lookup.
* **General questions** go to the AI Agent (Ollama, `qwen3:4b-instruct`)
  with a short window memory keyed by the session id.
* **A miss is not an error.** A missing order produces
  "I couldn't find order #1001", a database failure produces a temporary
  error message, and a single misheard digit produces
  "Did you mean order #1001?".
* The lookup node uses **Always Output Data** so a miss does not stop the
  workflow, and its **error output** is handled separately, so a database
  failure can never be reported as "order not found".

## The order parser

`workflow/extract_order_id.js` is the only source of truth for order
numbers. It is embedded into the workflow by `build_voice_workflow.py` and
unit tested with Node, so the tested code is the code that runs in n8n.

| Input | Result |
|---|---|
| `order 1001`, `order 1,001`, `order #1001` | 1001, confident |
| `one thousand and one` | 1001, confident |
| `1,000 and 1`, `1000 and a 1` | 1001, recovered from a compound |
| `order 1001 and order 1002` | asks which order to check |
| `order 1000 at 1` | asks the customer to repeat the number |
| `14 days`, `2026`, phone numbers | not an order number |

The parser returns a **route** (`order`, `general`, `ambiguous`,
`uncertain`, `empty`) and a **confidence** (`exact`, `recovered`), so the
workflow can answer a confident miss differently from an uncertain reading.

## The endpointer

`vad_stream.py` is a resumable state machine: one microphone stream produces
utterance after utterance, and the learned room level survives between turns.

* The **noise floor** is the median of the frames close to the quietest level
  seen. It is never a median over all frames, because that would sit at the
  level of the quiet parts of the speech and cut the utterance short.
* **WebRTC VAD decides what is speech.** Its state is reset regularly,
  because after real speech it keeps reporting room tone as speech for
  several seconds (its re-adaptation).
* The **energy gate** removes frames that sit on the noise floor, so the VAD
  re-adaptation cannot keep the recording open. A frame that is not flat is
  moving, so it is treated as speech even when it is close to the background.
* If the VAD reports speech while the level neither moves nor rises above
  the background, it is stuck on noise. In a room with a low signal to noise
  ratio the safety net lets the level decide, so the recording always ends.

## Turn taking

* **Call mode** keeps the microphone open and answers automatically.
* While the agent talks the microphone is ignored (half duplex), so the
  answer can never trigger itself. With `--barge-in` the microphone stays
  live, the echo level is measured at the start of the playback and a
  sustained louder signal ends the answer.
* A goodbye ("have a nice day", "that's all", "hoşçakal") ends the call
  without waking the language model.

## Configuration

All settings are environment variables; the most useful ones can also be
given as command line flags (`voice_agent.py --help`).

| Variable | Default | Purpose |
|---|---|---|
| `N8N_WEBHOOK_URL` | `http://localhost:5678/webhook/voice-agent` | the voice backend |
| `AUDIO_DEVICE` | `:0` | AVFoundation input, e.g. `:0` |
| `AUDIO_SAMPLE_RATE` | `16000` | capture rate |
| `VAD_SILENCE_MS` | `800` | silence that ends a turn |
| `VAD_MAX_SECONDS` | `15` | longest single turn |
| `VAD_MIN_SPEECH_MS` | `300` | shorter speech is ignored |
| `VAD_AGGRESSIVENESS` | `3` | WebRTC VAD 0-3, 3 rejects room tone |
| `VAD_RESET_FRAMES` | `30` | how often the VAD state is reset |
| `VAD_NOISE_MARGIN_DB` | `6` | how far above the background speech must be |
| `VAD_BACKGROUND_BAND_DB` | `12` | band above the quietest level used to find the noise floor |
| `VAD_FLATNESS_DB` | `2` | level change below which a frame counts as steady |
| `VAD_EMA_ALPHA` | `0.4` | smoothing of the level tracker |
| `VAD_SNR_ARM_DB` | `20` | below this signal to noise ratio the safety net is armed |
| `VAD_SNR_GAP_DB` | `12` | how far below the speech level a frame counts as silence |
| `VAD_START_FRAMES` | `3` | voiced frames that confirm the start of speech |
| `VAD_NO_SPEECH_TIMEOUT_S` | `8` | give up when nobody starts talking |
| `VAD_CONFLICT_FRAMES` | `20` | sustained disagreement between the VAD and the level before the safety net takes over |
| `VAD_PREROLL_MS` | `300` | audio kept before the onset |
| `VAD_KEEP_TRAILING_SILENCE_MS` | `200` | silence kept after the last word |
| `PTT_DURATION` | `5` | length of a fixed recording |
| `WHISPER_CLI` | `/opt/homebrew/bin/whisper-cli` | the transcription binary |
| `WHISPER_MODEL` | `ggml-base.en.bin` | the model file |
| `WHISPER_PROMPT` | *(empty)* | optional decoding hint, can bias digits |
| `KOKORO_REPO` | `mlx-community/Kokoro-82M-bf16` | the voice model |
| `KOKORO_VOICE` | `af_heart` | the voice |
| `KOKORO_SPEED` | `1.0` | speaking rate |
| `KOKORO_LANG` | `a` | language code of the voice |
| `N8N_TIMEOUT_S` | `60` | timeout of the webhook request |
| `PLAYBACK_TIMEOUT_S` | `120` | timeout of the audio playback |
| `VOICE_MODE` | `call` | `call`, `arm` or `ptt` |
| `OUTPUT_DEVICE` | *(empty)* | play the answer on this device |
| `BARGE_IN` | `0` | allow the customer to interrupt |
| `BARGE_IN_DB` | `8` | how far above the echo the voice has to be to interrupt |
| `BARGE_IN_MS` | `250` | how long that has to last to count as an interruption |
| `FAREWELL_ENABLED` | `1` | end the call on a goodbye |
| `FAREWELL_LINE` | *(built in)* | what the agent says when hanging up |
| `LATENCY_LOG` | `logs/latency.jsonl` | per turn timings |

## Measured latency

On a Mac Mini M4 with a Bluetooth headset:

| Stage | Time |
|---|---|
| device open (once per call) | 0.19 s |
| end of speech detection | threshold + one frame (e.g. 810 ms) |
| whisper.cpp | 0.25 - 0.55 s |
| n8n order lookup | 0.02 - 0.08 s |
| n8n general question | 0.8 - 1.5 s |
| Kokoro (warm model) | 0.3 - 0.6 s |
| **time to first audio after the customer stops** | **about 1.0 s** |
