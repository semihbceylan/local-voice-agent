# Troubleshooting

## The agent answers with a different persona

The language model is small (`qwen3:4b-instruct`) and will invent a personal
name if the system prompt does not forbid it. The workflow pins the identity
("DemoShop Support"), forbids emoji and replaces any answer to a question
about the agent's own name. If you change the system prompt, keep those
three rules.

## Emoji in the answer, "words count mismatch" warnings

Emoji cannot be spoken. The phonemiser warns about them and the text to
speech engine reads them as noise. The workflow strips them before the
answer reaches Kokoro. If you see the warning, the answer came from a path
that bypasses the "Normalize AI Response" node.

## The recording stops in the middle of a sentence

The endpointer treats a quiet but real part of the speech as background. The
background estimate must be the real noise floor. Two things break it:

* A background estimated from "flat" frames picks up the quiet parts of the
  speech. The endpointer uses the frames close to the quietest level instead.
* The WebRTC VAD reports room tone as speech for several seconds after real
  speech. Its state is reset regularly so it cannot keep the recording open.

Debug it with the frame by frame trace:

```bash
VAD_DEBUG=1 .venv/bin/python tests/live_mic_test.py --device :0
```

## The recording runs far too long

The capture is bounded by `--max-seconds` (15 s by default). If it always
hits that limit, the microphone noise is high enough that the VAD cannot
separate it from speech. Check the reported background level: a quiet room
shows around -60 dBFS or lower. A busy room needs the safety net, which
needs a low signal to noise ratio.

## The order number comes back wrong

* `1,000 and 1` is understood as 1001 when the order context is clear.
* A number that is not close to any order produces
  "I couldn't find order #1011. Did you mean order #1001?" - the agent
  asks, it never guesses.
* If Whisper really mishears a digit, the answer is a suggestion, not a
  wrong order.

## No audio from the answer

```bash
.venv/bin/python voice_agent.py --list-output-devices
```

Then pick one with `--output-device "name"`. The device is opened once at
start-up, so the first answer is not delayed by the device activation.

## "Invalid audio device index"

```bash
ffmpeg -hide_banner -f avfoundation -list_devices true -i ""
```

Use the index that is printed. The Bluetooth headset usually shows up as
`[0]`.

## Nothing is recognised

The model file is not part of the repository. Run `./scripts/setup.sh`, or
download it manually:

```bash
curl -fL -o ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

## The n8n API key was exposed

Rotate it in n8n (Settings -> n8n API), update `.env`, and delete the old
executions that contain the old workflow version. The key is never stored in
this repository: `.env` is ignored and only `.env.example` is committed.
