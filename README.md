# Build an Agora voice agent with AssemblyAI's Voice Agent API

Run a server-side bot that joins an [Agora](https://www.agora.io/) RTC channel, listens to the caller's microphone, and replies with synthesized speech — all powered by a single WebSocket to AssemblyAI's **Voice Agent API**. No separate STT, LLM, or TTS services to wire up.

Agora handles the WebRTC transport (browser/mobile/desktop clients connecting through their global SD-RTN edge network). AssemblyAI handles the entire conversational AI pipeline: streaming speech-to-text on Universal-3 Pro, the LLM that decides what to say, and the voice that speaks it back. Turn detection, barge-in, and tool calling are built in.

## Why combine Agora and the Voice Agent API

Agora gives you battle-tested WebRTC: low-latency audio routing across 200+ countries, automatic codec negotiation, jitter buffers, NAT traversal, and SDKs for every client platform. What Agora doesn't ship is the AI brain — you bring your own.

The Voice Agent API is that brain in one connection. Stream PCM audio in over `wss://agents.assemblyai.com/v1/ws`, get PCM audio back. No STT vendor, no LLM vendor, no TTS vendor, no glue layer to maintain.

|  | DIY pipeline behind Agora | Voice Agent API behind Agora |
|---|---|---|
| Vendors to integrate | STT + LLM + TTS (3+) | 1 |
| API keys to rotate | 3+ | 1 |
| Round trips per turn | 3 | 1 |
| Turn detection | Plug in a separate VAD | Built in (acoustic + linguistic) |
| Barge-in | Implement yourself | Built in |
| Tool calling | Wire LLM tools yourself | Built in |
| Voices | Pick a TTS vendor | 18 English + 16 multilingual |

## Architecture

```
Browser / mobile client (Agora Web/iOS/Android SDK)
        │
        │  WebRTC audio over Agora SD-RTN
        ▼
  Agora RTC channel ──────────── this bot (server)
                                       │
                                       │  agora-python-server-sdk
                                       │  PcmAudioFrame, 16 kHz mono PCM16
                                       ▼
                                 resample to 24 kHz
                                       │
                                       │  base64 input.audio chunks
                                       ▼
                          wss://agents.assemblyai.com/v1/ws
                          ┌───────────────────────────────┐
                          │  AssemblyAI Voice Agent API   │
                          │  ├── Universal-3 Pro Streaming │
                          │  ├── LLM                       │
                          │  └── TTS (24 kHz PCM16)        │
                          │  + neural turn detection       │
                          │  + barge-in handling           │
                          │  + tool calling                │
                          └───────────────────────────────┘
                                       │
                                       │  base64 reply.audio chunks
                                       ▼
                                 resample to 16 kHz
                                       │
                                       │  AudioPcmDataSender.send_audio_pcm_data
                                       ▼
                                 Agora RTC channel
                                       │
                                       ▼
                          Browser / mobile client (hears the bot)
```

The bot is one Python process. It joins the channel as a publishing participant, runs an audio-frame observer to capture the caller's voice, and pushes the agent's reply back through Agora's `AudioPcmDataSender` so every other participant in the channel hears it.

## Prerequisites

- Python 3.10+
- An [Agora project](https://console.agora.io) with an **App ID**. If you've enabled App Certificate (recommended), you'll also need the certificate to mint join tokens.
- An [AssemblyAI API key](https://www.assemblyai.com/dashboard/signup) — free tier available.
- Linux or macOS (the Agora native server SDK that `agora-python-server-sdk` wraps does not officially ship Windows wheels). For Windows, run the bot inside WSL2 or a Linux container.

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/kelsey-aai/voice-agent-agora
cd voice-agent-agora

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```
ASSEMBLYAI_API_KEY=your_assemblyai_key
AGORA_APP_ID=your_agora_app_id
AGORA_APP_CERTIFICATE=your_agora_app_certificate
AGORA_CHANNEL=voice-agent-demo
AGORA_BOT_UID=9999
```

If your Agora project has App Certificate disabled, leave `AGORA_APP_CERTIFICATE` blank — the bot will join with an empty token.

### 3. Run the bot

```bash
python bot.py --channel voice-agent-demo
```

You should see:

```
INFO Joined Agora channel 'voice-agent-demo' as uid=9999
INFO AssemblyAI session ready: sess_abc123
```

### 4. Connect a client

Open Agora's [Web demo](https://webdemo.agora.io/basicVideoCall/index.html), enter your App ID, the same channel name, a different UID, and click **Join**. Speak — the bot transcribes you live, the LLM replies, and the synthesized voice plays back through your browser. The bot's transcript log streams to your terminal.

## How it works

The bridge is two cooperating asyncio tasks — one pulling caller audio out of Agora and pushing it to AssemblyAI, the other pulling reply audio out of AssemblyAI and pushing it back into Agora.

### 1. Connect to the Voice Agent API

```python
URL = "wss://agents.assemblyai.com/v1/ws"
headers = {"Authorization": f"Bearer {API_KEY}"}

async with websockets.connect(URL, additional_headers=headers) as ws:
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "system_prompt": "You are a friendly voice assistant joining a live Agora call. Keep replies short.",
            "greeting": "Hi — I just joined the call. What would you like help with?",
            "input": {"format": {"encoding": "audio/pcm"}},
            "output": {
                "voice": "ivy",
                "format": {"encoding": "audio/pcm"},
            },
        },
    }))
```

`session.update` is the first message and configures personality, greeting, and voice. The default audio format is `audio/pcm` — 24 kHz, 16-bit signed LE, mono — which we make explicit.

### 2. Pull caller audio out of Agora

The bot registers an `IAudioFrameObserver` whose `on_playback_audio_frame_before_mixing` hook fires every 10 ms with one participant's audio frame. We resample 16 kHz → 24 kHz with SciPy's polyphase filter and queue the bytes for the WebSocket sender:

```python
def on_playback_audio_frame_before_mixing(self, channel_id, uid, frame):
    pcm16 = bytes(frame.buffer)              # 16 kHz PCM16
    pcm24 = resample_pcm16(pcm16, 16_000, 24_000)
    loop.call_soon_threadsafe(agent.inbound_audio.put_nowait, pcm24)
    return 0
```

`call_soon_threadsafe` is required because Agora's observer runs on a native C++ thread, not the asyncio loop.

### 3. Stream audio to AssemblyAI

A single coroutine drains the queue and ships each chunk as an `input.audio` event:

```python
chunk = await mic_queue.get()
await ws.send(json.dumps({
    "type": "input.audio",
    "audio": base64.b64encode(chunk).decode(),
}))
```

### 4. Publish the reply back into Agora

When `reply.audio` events arrive, we decode the base64 PCM, resample 24 kHz → 16 kHz, and hand it to `AudioPcmDataSender`:

```python
elif t == "reply.audio":
    pcm = base64.b64decode(event["data"])
    await self.outbound_audio.put(pcm)
```

```python
pcm16 = resample_pcm16(pcm24, 24_000, 16_000)
self.pcm_sender.send_audio_pcm_data(
    pcm16, 0, len(pcm16)//2, 2, 1, 16_000,
)
```

We pace the pushes to wall-clock time so a long reply doesn't blast into Agora's buffer in one go — that lets barge-in stay responsive.

### 5. Handle barge-in

When the caller talks over the bot, the server emits `reply.done` with `status: "interrupted"`. We flush the outbound queue so the bot stops speaking immediately:

```python
elif t == "reply.done" and event.get("status") == "interrupted":
    while not outbound_audio.empty():
        outbound_audio.get_nowait()
```

The Voice Agent API also trims the `transcript.agent` event to what the bot actually got out before it was cut off — useful for accurate logging.

## Tuning

### Pick a different voice

Drop any voice ID into `session.output.voice`:

```python
"output": {"voice": "james"}    # conversational US male
"output": {"voice": "sophie"}   # clear UK female
"output": {"voice": "diego"}    # Latin American Spanish
"output": {"voice": "arjun"}    # Hindi/Hinglish
```

Multilingual voices code-switch with English automatically. Browse the full [Voices catalog](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices).

### Adjust turn detection

For noisy call environments, raise `vad_threshold`. For deliberate speakers, raise `max_silence`:

```python
"input": {
    "turn_detection": {
        "vad_threshold": 0.5,
        "min_silence": 600,
        "max_silence": 1500,
        "interrupt_response": True,
    }
}
```

### Boost domain-specific words

Bias recognition toward names, products, or jargon that show up on your calls:

```python
"input": {"keyterms": ["AssemblyAI", "Agora", "Universal-3"]}
```

### Add tools

Register functions on `session.tools` to let the agent look up data, hit APIs, or trigger workflows. The agent emits a `tool.call` event with the call_id and arguments; you run the function and return a `tool.result` event after the next `reply.done`. Full pattern in the [tool calling docs](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tool-calling).

## Troubleshooting

**`agora-python-server-sdk` install fails on macOS.** The package ships pre-built C++ wheels for Linux and macOS x86_64/arm64. If pip falls back to source build, install Xcode command-line tools (`xcode-select --install`) or run the bot in a Linux container.

**Bot joins but stays silent.** Check that your client connected with the same `AGORA_CHANNEL` name and a **different** UID than `AGORA_BOT_UID`. Agora rejects duplicate UIDs.

**`UNAUTHORIZED` close from AssemblyAI.** API key missing, expired, or wrong. Pull a fresh one from the [AssemblyAI dashboard](https://www.assemblyai.com/dashboard/signup) and double-check `.env`.

**Audio sounds chipmunky or sluggish.** Sample-rate mismatch. Confirm `set_playback_audio_frame_before_mixing_parameters(channels=1, sample_rate_hz=16000)` and that resampling is on between Agora's 16 kHz and the Voice Agent API's 24 kHz.

**Bot interrupts itself.** Acoustic loop somewhere — usually one client has speakers + mic open without echo cancellation. Browser clients should request `getUserMedia({ audio: { echoCancellation: true } })`.

**Token errors from Agora.** If your project has App Certificate enabled, `AGORA_APP_CERTIFICATE` must be set and the bot UID + channel name must match what you signed.

Full Voice Agent API troubleshooting guide: [docs.assemblyai.com](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting).

## Known limitations

- **`agora-python-server-sdk` is a beta wrapper** around Agora's native C++ SDK. Class layouts have moved between minor versions. We pin `2.2.4` and document the exact API surface (`AgoraService`, `RTCConnection`, `IAudioFrameObserver`, `AudioPcmDataSender`) the bot uses. If you upgrade, the `# AGORA_BRIDGE_TODO` comments in `bot.py` mark the four call sites you may need to re-bind.
- **Agora's recommended path for new voice-agent projects is the [Conversational AI Engine](https://docs.agora.io/en/conversational-ai/overview/product-overview)** — a hosted REST service that handles the full STT/LLM/TTS pipeline inside Agora's cloud. AssemblyAI is supported there as STT, but the LLM and TTS layers are Agora's. Use this tutorial when you want to keep the entire AI stack on AssemblyAI's Voice Agent API rather than splitting it.
- **No Windows wheels.** Run inside WSL2 or a Linux Docker container.

## Related tutorials

- **Voice Agent API 5-minute quickstart** — minimal Python quickstart — the simplest version of this stack
- **Build a voice assistant app** — browser UI on top of the Voice Agent API with temporary-token auth
- **Voice agent that makes outbound calls** — Twilio outbound + Media Streams bridge with `audio/pcmu`
- **Twilio phone agent** — inbound phone calls — Twilio Media Streams + `audio/pcmu`
- **LiveKit voice agent** — Voice Agent API behind a LiveKit room for multi-user / WebRTC
- **Daily.co voice agent** — Voice Agent API behind a Daily room using `daily-python`
- **Node.js voice agent** — JavaScript version of the 5-minute quickstart
- **Raw WebSocket voice agent** — every protocol event handled explicitly, with tool calling and session resume

## FAQ

### What is the AssemblyAI Voice Agent API?

The AssemblyAI Voice Agent API is a single WebSocket endpoint (`wss://agents.assemblyai.com/v1/ws`) that handles the entire voice agent pipeline server-side — speech recognition on Universal-3 Pro Streaming, LLM reasoning, and text-to-speech with 30+ voices. It includes neural turn detection, barge-in, and tool calling. You stream PCM audio in and get PCM audio back, so you don't have to wire up separate STT, LLM, and TTS providers.

### How do I connect AssemblyAI's Voice Agent API to Agora?

Run a server-side bot built on `agora-python-server-sdk`. The bot joins the Agora RTC channel as a publishing participant, registers an `IAudioFrameObserver` to capture caller audio (16 kHz PCM frames), resamples to 24 kHz, and forwards each chunk to `wss://agents.assemblyai.com/v1/ws` as base64-encoded `input.audio` events. Reply audio comes back as `reply.audio` events, gets resampled to 16 kHz, and is published into the channel via `AudioPcmDataSender`. The full bridge fits in one Python file — see `bot.py` in this repo.

### Can I use Agora's Conversational AI Engine instead?

Yes — Agora's [Conversational AI Engine](https://docs.agora.io/en/conversational-ai/overview/product-overview) is a hosted alternative that takes care of joining the channel and bridging to STT/LLM/TTS for you, and it supports AssemblyAI as the speech-to-text provider. Use it when you're happy to use Agora's LLM and TTS layers. Use this tutorial's pattern when you want the full AI pipeline (STT + LLM + TTS) on AssemblyAI's Voice Agent API in one connection.

### What audio format does the Voice Agent API use with Agora?

The Voice Agent API defaults to `audio/pcm` — 24 kHz, 16-bit signed little-endian, mono. Agora delivers and accepts 16 kHz mono PCM through the `agora-python-server-sdk`, so the bot resamples 16 kHz ↔ 24 kHz on each side of the bridge using SciPy's polyphase filter (`scipy.signal.resample_poly`). Both audio formats are documented in the [audio format reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/audio-format).

### How does barge-in work between Agora and the Voice Agent API?

The Voice Agent API has built-in turn detection. When the caller speaks while the bot is replying, the server emits `reply.done` with `status: "interrupted"` and trims the agent transcript to what was actually spoken. The bridge in this tutorial flushes its outbound audio queue on that event so the bot stops talking immediately, instead of continuing to publish queued reply audio after the user has cut in.

### Do I need an Agora App Certificate to run the bot?

Only if your Agora project has App Certificate enabled. If it's enabled, set `AGORA_APP_CERTIFICATE` in `.env`; the bot uses `agora-token-builder` to mint a 1-hour publisher token at startup. If App Certificate is disabled, leave it blank — Agora accepts an empty token. App Certificate is recommended for production.

### How much does the AssemblyAI Voice Agent API cost?

AssemblyAI offers a free tier so you can build and test without a credit card. For current pricing, see the [AssemblyAI pricing page](https://www.assemblyai.com/pricing).

## Resources

- [Voice Agent API overview](https://www.assemblyai.com/docs/voice-agents/voice-agent-api)
- [Session configuration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration)
- [Audio format](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/audio-format)
- [Events reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference)
- [Voices catalog](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices)
- [Tool calling](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tool-calling)
- [Browser integration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/browser-integration)
- [Troubleshooting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting)
- [Agora Python Server SDK on PyPI](https://pypi.org/project/agora-python-server-sdk/)
- [Agora docs](https://docs.agora.io/en/)

---

<div class="blog-cta_component">
  <div class="blog-cta_title">Ship your Agora voice agent today</div>
  <div class="blog-cta_rt w-richtext">
    <p>Sign up for a free AssemblyAI account, drop your key into <code>.env</code>, and the bot in this tutorial will join an Agora channel and start replying in minutes. No credit card required.</p>
  </div>
  <a href="https://www.assemblyai.com/dashboard/signup" class="button w-button">Start building</a>
</div>

<div class="blog-cta_component">
  <div class="blog-cta_title">Hear every Voice Agent API voice</div>
  <div class="blog-cta_rt w-richtext">
    <p>Compare 18 English voices and 16 multilingual voices side by side, then drop your pick straight into <code>session.output.voice</code>.</p>
  </div>
  <a href="https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices" class="button w-button">Browse voices</a>
</div>
