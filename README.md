# Build an Agora voice agent with AssemblyAI speech-to-text and text-to-speech

Run a server-side bot that joins an [Agora](https://www.agora.io/) RTC channel like any other participant, listens to the caller, and answers out loud. Agora carries the audio; the bot's Python code owns the conversation loop, on three AssemblyAI APIs that share one key:

| Layer | Service | What it does in this bot |
|---|---|---|
| Transport | Agora RTC channel | The bot joins with Agora's Python Server Gateway SDK, subscribes to the caller's audio, and publishes its own audio track |
| Listening | [Universal-3.6 Pro Realtime](https://www.assemblyai.com/blog/universal-3-6-pro-realtime) | Streaming speech-to-text with built-in turn detection: `Turn` events with `end_of_turn` |
| Thinking | [LLM Gateway](https://www.assemblyai.com/docs/llm-gateway/quickstart) | OpenAI-compatible chat completions; swap in any model from the [available models](https://www.assemblyai.com/docs/llm-gateway/available-models) list |
| Speaking | [AssemblyAI Text-to-Speech](https://www.assemblyai.com/products/text-to-speech) | One streaming session for the whole call, returning 16 kHz PCM that goes straight into Agora |
| Barge-in | Your code | Real speech during a reply cancels the speech request and clears Agora's outbound buffer |

This is the companion repo for the tutorial: [Build an Agora voice agent with AssemblyAI speech-to-text and text-to-speech](https://www.assemblyai.com/blog/build-agora-voice-agent-quickstart).

## Architecture

```
Caller (browser or mobile app on the Agora SDK)
   │  WebRTC
   ▼
Agora channel
   │  16 kHz PCM frames from the caller
   ▼
bot.py (Agora Server Gateway SDK, joined as uid 9999)
   │
   ├─► Universal-3.6 Pro Realtime ──► Turn events (end_of_turn)
   │
   ├─► LLM Gateway ──► reply text
   │
   ├─► AssemblyAI Text-to-Speech session ──► 16 kHz PCM
   │
   ▼  push_audio_pcm_data()
Agora channel ──► the caller hears the reply
```

Every hop runs at 16 kHz, 16-bit, mono PCM. Agora delivers the caller's audio at 16 kHz, Universal-3.6 Pro Realtime accepts it as is, and the text-to-speech session is opened with `sample_rate=16000`, so the server returns audio at the rate Agora publishes. The model's native output is 24 kHz; the service resamples on its side, so the bot needs no resampling code.

## Prerequisites

- Python 3.10+ (Agora's Python Server Gateway SDK requires it)
- A Linux x86-64 server for production (Agora lists Ubuntu 18.04+ and CentOS 7+). The SDK also runs on macOS 13+ for development and testing. There's no Windows build; use WSL2 or a Linux container.
- An [Agora project](https://console.agora.io) with an App ID, and a temporary RTC token if App Certificate is enabled
- An [AssemblyAI API key](https://www.assemblyai.com/dashboard/signup). LLM Gateway uses the same key; check that your plan includes it.

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/kelsey-aai/voice-agent-agora
cd voice-agent-agora

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins `agora_python_server_sdk==2.4.10`, the version `bot.py` is written against, and installs `assemblyai>=1.8.2` (text-to-speech is in the Python SDK from 1.8.2).

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```
ASSEMBLYAI_API_KEY=your_assemblyai_api_key
AGORA_APP_ID=your_agora_app_id
# Leave empty if your Agora project has App Certificate disabled
AGORA_TOKEN=
AGORA_CHANNEL=voice-agent-demo
AGORA_BOT_UID=9999
```

If your Agora project has App Certificate disabled, leave `AGORA_TOKEN` blank. If it's enabled, generate a temporary RTC token in the Agora Console for the same channel name and the bot's UID (9999), as described in Agora's [Server Gateway Python quickstart](https://docs.agora.io/en/realtime-media/rtc-server-sdk/quickstart/python). Temporary tokens expire, so mint them on your server in production.

### 3. Run the bot

```bash
python bot.py
```

The bot joins the channel and waits for someone to speak before it greets them:

```
Joined Agora channel 'voice-agent-demo' as uid 9999
Waiting for someone to join and speak...
Caller uid 1234 is on the line
Speech-to-text session 7f1c...
Bot:    Hi, I just joined the call. What can I help you with?
Caller: Can you hear me okay?
Bot:    I can hear you clearly. What would you like to do today?
```

### 4. Connect a client

Open Agora's [Web demo](https://webdemo.agora.io/basicVideoCall/index.html), enter your App ID, the same channel name, and a different UID, and click **Join**. If App Certificate is enabled, the demo needs its own token for its UID. Speak, and the bot answers through your browser: Universal-3.6 Pro Realtime transcribes you, the LLM writes the reply, and AssemblyAI Text-to-Speech speaks it into the channel.

## How it works

`bot.py` is one file, in six sections:

1. **Set up the three AssemblyAI clients.** A `RealTimeTranscriber` listens, an `LLMGateway` client thinks, and one `TTSSession` speaks for the whole call, taking one request per reply. The voice is set explicitly to Audrey (`49c443a4-8a1d-5f7e-b70b-98bdbd9823c3`) and the session asks for 16 kHz `pcm_s16le`.
2. **Join the channel and hear the caller.** The connection uses the `AUDIO_SCENARIO_AI_SERVER` scenario and a PCM publish config. An `IAudioFrameObserver` receives each remote user's 16 kHz frames via `on_playback_audio_frame_before_mixing`. Agora calls it on a native thread, so the callback only copies frames into a queue; `chunks()` batches them into 50 ms chunks for speech-to-text. The bot follows the first participant who speaks.
3. **Publish the bot's voice.** `push_audio_pcm_data()` takes PCM in whole milliseconds (multiples of 32 bytes at 16 kHz mono), so `BotVoice` carries any partial millisecond over and pads the last one with silence. `is_push_to_rtc_completed()` reports when Agora's buffer has played out, and `interrupt_audio()` drops whatever is still queued.
4. **Listen for turns and detect barge-in.** Universal-3.6 Pro Realtime sends `Turn` events; one with `end_of_turn` set goes to the main loop. While the bot is speaking, a transcript with real content (not a backchannel like "mhm") sets the interrupted flag.
5. **Think, then speak with barge-in.** `think()` sends the conversation to LLM Gateway. `speak()` streams the reply through the TTS session into Agora; if the caller cuts in, it calls `tts.cancel()` and `voice.stop()`.
6. **Run the main loop.** Wait for a caller, connect both AssemblyAI sessions, stream the caller's audio on a background thread, greet, then answer each turn. A TTS session closes after two idle minutes, so the loop sends `keep_alive()` when the line is quiet.

## Tuning

### Pick a different voice

```python
tts = aai.TTSSession(
    aai.TTSConfig(
        voice_id="aai-sara",  # copy any voice_id from the voice library
        language="spanish",
        sample_rate=SAMPLE_RATE,
        encoding="pcm_s16le",
    ),
    api_key=API_KEY,
)
```

Browse the [voice library](https://www.assemblyai.com/docs/tts/voices) for thousands of voices across six languages: English, Spanish, French, German, Italian, and Portuguese. Copy a voice's `voice_id` and set `language` to match your callers. Universal-3.6 Pro Realtime transcribes 32 languages; to steer it toward your callers' language, pass `language_codes` when you connect:

```python
    stt.connect(
        RealTimeParameters(
            speech_model="universal-3-6-pro",
            sample_rate=SAMPLE_RATE,
            language_codes=["es"],  # steer speech-to-text toward Spanish
        )
    )
```

### Adjust turn detection

Start with the defaults. They suit conversational turns, and the bot doesn't set any turn-detection parameters. If your callers dictate account numbers or email addresses and get cut off mid-entity, the [voice agent best practices](https://www.assemblyai.com/docs/voice-agents/best-practices) explain the silence settings and how to widen them only for that part of the call.

### Boost domain-specific words

```python
    stt.connect(
        RealTimeParameters(
            speech_model="universal-3-6-pro",
            sample_rate=SAMPLE_RATE,
            keyterms_prompt=["Agora", "AssemblyAI", "Acme Cloud", "Priya Raman"],
        )
    )
```

See [keyterms prompting](https://www.assemblyai.com/docs/streaming/keyterms-prompting) for limits and tips.

### Add tools

Tool calling lives in the LLM request. Pass OpenAI-style tool schemas as `tools`, run the function the model asks for, and send the result back. This version of `think()` replaces the one in `bot.py`:

```python
# Replaces think() in bot.py. Add "import json" to the imports at the top.
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}]


def get_weather(city):
    return f"Sunny and 22 degrees in {city}."  # call a real weather API here


def think(user_text):
    messages.append({"role": "user", "content": user_text})
    for _ in range(3):  # at most three tool rounds per turn
        completion = llm.chat.completions.create(
            model=LLM_MODEL, messages=messages, tools=TOOLS, max_tokens=200
        )
        text = " ".join(c.message.content for c in completion.choices if c.message.content).strip()
        calls = [call for c in completion.choices for call in (c.message.tool_calls or [])]
        if not calls:
            messages.append({"role": "assistant", "content": text})
            return text
        messages.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
        })
        for call in calls:
            args = json.loads(call.function.arguments or "{}")
            result = get_weather(**args) if call.function.name == "get_weather" else "Unknown tool"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "Sorry, I couldn't finish that. Could you ask again?"
```

Full pattern in the [LLM Gateway tool calling docs](https://www.assemblyai.com/docs/llm-gateway/tool-calling).

### Start speaking sooner

The bot waits for the whole LLM reply before it speaks. To cut the wait, stream the model's tokens into the text-to-speech session with `generate()` and finish each reply with `flush()`; the server starts speaking once it holds enough complete sentences. The [text-to-speech quickstart](https://www.assemblyai.com/docs/tts/getting-started/quickstart) shows the pattern, and [optimizing accuracy and latency](https://www.assemblyai.com/docs/tts/getting-started/optimizing-accuracy-and-latency) covers the rest.

## Troubleshooting

**`agora_python_server_sdk` install fails.** The SDK supports Linux and macOS with Python 3.10+. On Windows, run the bot inside WSL2 or a Linux container. On macOS, install the Xcode command-line tools (`xcode-select --install`) if pip needs to build anything.

**Bot joins but never greets.** The bot greets once someone in the channel speaks. Check that your client joined the same `AGORA_CHANNEL` with a different UID than `AGORA_BOT_UID`, and that its microphone is unmuted.

**Error 1008 from AssemblyAI.** The API key is missing or invalid, or the account has a problem such as insufficient funds. Check `ASSEMBLYAI_API_KEY` in `.env`. See [error handling](https://www.assemblyai.com/docs/tts/error-handling) for every close code.

**Audio sounds chipmunky or slowed down.** A sample-rate mismatch. The Agora frame parameters, the speech-to-text `sample_rate`, and the text-to-speech `sample_rate` must all be 16000, as `SAMPLE_RATE` sets them.

**Bot interrupts itself.** An acoustic loop: the caller's microphone picks up the bot's voice from their speakers. Browser clients should request `getUserMedia({ audio: { echoCancellation: true } })`, and headphones fix it for testing.

**Token errors from Agora.** With App Certificate enabled, `AGORA_TOKEN` must be a valid token for this channel name and the bot's UID, and it must not have expired.

## Known limitations

- **`agora_python_server_sdk` wraps Agora's native C++ SDK, and its Python API has changed between releases**: in the 2.4 line, publishing and PCM pushes moved onto `RTCConnection`. This bot uses the 2.4.10 API as shown in the [SDK's own examples](https://github.com/AgoraIO-Extensions/Agora-Python-Server-SDK). If you upgrade, check those examples first.
- **One caller per bot.** The bot follows the first participant who speaks. For group calls, run speech-to-text per participant and decide in your code who the bot should answer.
- **Agora also offers a hosted option.** Agora's Conversational AI Engine runs the voice pipeline as a hosted REST service. Use this repo when you want the conversation loop in your own code, on AssemblyAI's speech-to-text and text-to-speech.
- **No Windows build.** Run inside WSL2 or a Linux Docker container.

## Resources

- [Tutorial: Build an Agora voice agent with AssemblyAI speech-to-text and text-to-speech](https://www.assemblyai.com/blog/build-agora-voice-agent-quickstart)
- [Universal-3.6 Pro Realtime](https://www.assemblyai.com/blog/universal-3-6-pro-realtime)
- [Voice agent best practices](https://www.assemblyai.com/docs/voice-agents/best-practices)
- [LLM Gateway quickstart](https://www.assemblyai.com/docs/llm-gateway/quickstart)
- [Text-to-Speech quickstart](https://www.assemblyai.com/docs/tts/getting-started/quickstart)
- [Text-to-Speech voices](https://www.assemblyai.com/docs/tts/voices)
- [Text-to-Speech limits](https://www.assemblyai.com/docs/tts/limits)
- [Agora Python Server SDK on GitHub](https://github.com/AgoraIO-Extensions/Agora-Python-Server-SDK)
- [Agora Server Gateway Python quickstart](https://docs.agora.io/en/realtime-media/rtc-server-sdk/quickstart/python)
