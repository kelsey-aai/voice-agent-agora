# bot.py
import os
import queue
import string
import threading
import time

import assemblyai as aai
from assemblyai.streaming.v3 import (
    BeginEvent,
    RealTimeError,
    RealTimeEvents,
    RealTimeParameters,
    RealTimeTranscriber,
    RealTimeTranscriberOptions,
    TurnEvent,
)
from dotenv import load_dotenv

# Agora Server Gateway SDK (pip install agora_python_server_sdk)
from agora.rtc.agora_base import (
    AgoraServiceConfig,
    AudioPublishType,
    AudioScenarioType,
    AudioSubscriptionOptions,
    ChannelProfileType,
    ClientRoleType,
    RTCConnConfig,
    RtcConnectionPublishConfig,
)
from agora.rtc.agora_service import AgoraService
from agora.rtc.audio_frame_observer import IAudioFrameObserver

load_dotenv()
API_KEY = os.environ["ASSEMBLYAI_API_KEY"]
AGORA_APP_ID = os.environ["AGORA_APP_ID"]
AGORA_TOKEN = os.environ.get("AGORA_TOKEN", "")  # empty if App Certificate is off
AGORA_CHANNEL = os.environ.get("AGORA_CHANNEL", "voice-agent-demo")
AGORA_BOT_UID = os.environ.get("AGORA_BOT_UID", "9999")

# One rate end to end: Agora delivers 16 kHz frames, speech-to-text takes
# 16 kHz, and text-to-speech returns 16 kHz. No resampling in this bot.
SAMPLE_RATE = 16_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 16-bit mono: 32 bytes per millisecond

LLM_MODEL = "claude-haiku-4-5-20251001"  # any model on LLM Gateway
GREETING = "Hi, I just joined the call. What can I help you with?"
SYSTEM_PROMPT = f"""You are a friendly voice assistant on a live call.
Your replies are spoken aloud: one or two short sentences, no lists or markdown.
You have already greeted the caller with: {GREETING}"""

# Listen: Universal-3.6 Pro Realtime streaming speech-to-text
stt = RealTimeTranscriber(RealTimeTranscriberOptions(), api_key=API_KEY)

# Think: an LLM through LLM Gateway, on the same API key
llm = aai.LLMGateway(api_key=API_KEY)

# Speak: one text-to-speech session for the whole call, at Agora's 16 kHz
tts = aai.TTSSession(
    aai.TTSConfig(
        voice_id="49c443a4-8a1d-5f7e-b70b-98bdbd9823c3",  # Audrey
        language="english",
        sample_rate=SAMPLE_RATE,
        encoding="pcm_s16le",
    ),
    api_key=API_KEY,
)

messages = [{"role": "system", "content": SYSTEM_PROMPT}]
turns = queue.Queue()            # finished caller turns, in order
speaking = threading.Event()     # set while the bot's reply is playing in the channel
interrupted = threading.Event()  # set when the caller talks over the bot
last_spoke_at = 0.0
# bot.py (continued): joining the channel and hearing the caller
class CallerAudio(IAudioFrameObserver):
    """Receives each remote user's 16 kHz PCM frames, on an Agora SDK thread."""

    def __init__(self):
        super().__init__()
        self.frames = queue.Queue()
        self.caller_uid = None
        self.joined = threading.Event()

    def on_playback_audio_frame_before_mixing(
        self, agora_local_user, channelId, uid, frame, vad_result_state, vad_result_bytearray
    ):
        if self.caller_uid is None:
            self.caller_uid = uid  # follow the first person who speaks
            self.joined.set()
        if uid == self.caller_uid:
            # Copy only: Agora callbacks must not call SDK APIs or do heavy work
            self.frames.put(bytes(frame.buffer))
        return 1

    def chunks(self):
        """Yield 50 ms chunks for speech-to-text. Ends when None is queued."""
        buf = bytearray()
        while True:
            frame = self.frames.get()
            if frame is None:
                return
            buf += frame
            if len(buf) >= BYTES_PER_MS * 50:
                yield bytes(buf)
                buf.clear()


def join_agora(caller):
    service = AgoraService()
    config = AgoraServiceConfig()
    config.appid = AGORA_APP_ID
    config.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_AI_SERVER
    service.initialize(config)

    connection = service.create_rtc_connection(
        RTCConnConfig(
            client_role_type=ClientRoleType.CLIENT_ROLE_BROADCASTER,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
            auto_subscribe_audio=1,
            audio_subs_options=AudioSubscriptionOptions(
                pcm_data_only=1,
                bytes_per_sample=2,
                number_of_channels=1,
                sample_rate_hz=SAMPLE_RATE,
            ),
        ),
        RtcConnectionPublishConfig(
            audio_scenario=AudioScenarioType.AUDIO_SCENARIO_AI_SERVER,
            is_publish_audio=True,
            audio_publish_type=AudioPublishType.AUDIO_PUBLISH_TYPE_PCM,
        ),
    )
    ret = connection.connect(AGORA_TOKEN, AGORA_CHANNEL, AGORA_BOT_UID)
    if ret < 0:
        raise RuntimeError(f"Agora connect failed: {ret}")

    connection.get_local_user().set_playback_audio_frame_before_mixing_parameters(1, SAMPLE_RATE)
    connection.register_audio_frame_observer(caller, 0, None)  # 0: Agora's own VAD off
    connection.publish_audio()
    print(f"Joined Agora channel '{AGORA_CHANNEL}' as uid {AGORA_BOT_UID}")
    return service, connection

# bot.py (continued): publishing the bot's voice
class BotVoice:
    """Pushes reply audio into the channel as the bot's audio track."""

    def __init__(self, connection):
        self.connection = connection
        self.pending = bytearray()

    def push(self, pcm):
        # Agora takes PCM in whole milliseconds (32 bytes at 16 kHz mono)
        self.pending += pcm
        whole = len(self.pending) - len(self.pending) % BYTES_PER_MS
        if whole:
            self.connection.push_audio_pcm_data(bytes(self.pending[:whole]), SAMPLE_RATE, 1)
            del self.pending[:whole]

    def finish(self):
        if self.pending:  # pad the last partial millisecond with silence
            self.push(bytes(BYTES_PER_MS - len(self.pending)))

    def playing(self):
        return not self.connection.is_push_to_rtc_completed()

    def stop(self):
        self.pending.clear()
        self.connection.interrupt_audio()  # drop audio Agora has buffered
# bot.py (continued): listening and barge-in
BACKCHANNELS = frozenset({
    "mhm", "mm", "mmhm", "uh", "uhhuh", "huh", "um", "umm", "hmm", "hm",
    "ah", "oh", "yeah", "yep", "yup", "okay", "ok", "right", "alright", "gotcha",
})
_PUNCT = str.maketrans("", "", string.punctuation)


def is_filler(text, min_words):
    tokens = text.lower().translate(_PUNCT).split()
    return len(tokens) < min_words or all(t in BACKCHANNELS for t in tokens)


def on_begin(client, event: BeginEvent):
    print(f"Speech-to-text session {event.id}")


def on_turn(client, event: TurnEvent):
    text = event.transcript.strip()
    if not text:
        return
    if speaking.is_set():
        if is_filler(text, min_words=2):
            return             # "mhm", "yeah": let the bot keep talking
        interrupted.set()      # a real interruption: stop the reply
    elif time.monotonic() - last_spoke_at < 1.0 and is_filler(text, min_words=1):
        return                 # a trailing "mhm" right after the bot finished
    if event.end_of_turn:
        print(f"Caller: {text}")
        turns.put(text)


def on_error(client, error: RealTimeError):
    print(f"Speech-to-text error: {error}")
# bot.py (continued): thinking and speaking
def think(user_text):
    messages.append({"role": "user", "content": user_text})
    completion = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        max_tokens=200,
    )
    reply = " ".join(c.message.content for c in completion.choices if c.message.content).strip()
    messages.append({"role": "assistant", "content": reply})
    return reply


def speak(text, voice):
    global last_spoke_at
    if not text:
        return
    print(f"Bot:    {text}")
    interrupted.clear()
    speaking.set()
    try:
        for chunk in tts.speak(text):
            if interrupted.is_set():
                tts.cancel()  # barge-in: the server drops the rest of this reply
                break
            voice.push(chunk)
        else:
            voice.finish()
        # Synthesis runs faster than real time, so Agora holds a buffer.
        # Wait for it to play out, or for the caller to cut in.
        while voice.playing() and not interrupted.is_set():
            time.sleep(0.05)
        if interrupted.is_set():
            voice.stop()
            print("        (interrupted)")
    finally:
        speaking.clear()
        last_spoke_at = time.monotonic()
# bot.py (continued): the main loop
def main():
    caller = CallerAudio()
    service, connection = join_agora(caller)
    voice = BotVoice(connection)
    print("Waiting for someone to join and speak...")
    caller.joined.wait()
    print(f"Caller uid {caller.caller_uid} is on the line")

    stt.on(RealTimeEvents.Begin, on_begin)
    stt.on(RealTimeEvents.Turn, on_turn)
    stt.on(RealTimeEvents.Error, on_error)
    stt.connect(
        RealTimeParameters(
            speech_model="universal-3-6-pro",
            sample_rate=SAMPLE_RATE,
        )
    )
    tts.connect()
    threading.Thread(target=stt.stream, args=(caller.chunks(),), daemon=True).start()

    try:
        speak(GREETING, voice)
        while True:
            try:
                user_text = turns.get(timeout=30)
            except queue.Empty:
                tts.keep_alive()  # the TTS session closes after 2 idle minutes
                continue
            speak(think(user_text), voice)
    except KeyboardInterrupt:
        print("\nLeaving the channel...")
    finally:
        caller.frames.put(None)
        stt.disconnect(terminate=True)
        tts.terminate()
        connection.disconnect()
        connection.release()
        service.release()


if __name__ == "__main__":
    main()
