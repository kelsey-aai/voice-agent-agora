"""Agora <-> AssemblyAI Voice Agent API bridge bot.

A server-side bot that:
  1. Joins an Agora RTC channel as a publishing participant.
  2. Subscribes to the user's audio (16 kHz mono PCM via Agora's PcmAudioFrame).
  3. Resamples to 24 kHz and forwards it to the AssemblyAI Voice Agent API
     over a single WebSocket (STT + LLM + TTS handled server-side).
  4. Receives reply.audio chunks (24 kHz PCM16), resamples back to 16 kHz,
     and publishes them into the channel as the bot's outbound audio track.

Honest note on the Agora SDK
----------------------------
Agora's "agora-python-server-sdk" is a thin Python wrapper around the C++
RTC SDK. The exact class layout has churned across releases, and Agora's
recommended path for new voice-agent projects today is the
**Conversational AI Engine** (a hosted REST service that wires STT/LLM/TTS
into a channel for you). We pin a known release and document every Agora
API call we make. If your Agora SDK version names things differently,
the # AGORA_BRIDGE_TODO blocks below show exactly which surface area to
re-bind. The AssemblyAI side of the bridge is stable and unaffected.

Run:
    python bot.py --channel my-channel
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np
import websockets
from dotenv import load_dotenv
from scipy.signal import resample_poly

# ---------------------------------------------------------------------------
# Agora server SDK imports — pinned to agora-python-server-sdk==2.2.4.
# If you upgrade, verify the symbols below still exist; otherwise update
# the # AGORA_BRIDGE_TODO sections.
# ---------------------------------------------------------------------------
try:
    from agora.rtc.agora_service import AgoraService, AgoraServiceConfig
    from agora.rtc.rtc_connection import RTCConnection, RTCConnConfig
    from agora.rtc.audio_frame_observer import IAudioFrameObserver, AudioFrame
    from agora.rtc.audio_pcm_data_sender import AudioPcmDataSender
    from agora.rtc.local_user_observer import IRTCLocalUserObserver
    from agora.rtc.media_node_factory import MediaNodeFactory
    AGORA_AVAILABLE = True
except ImportError:  # pragma: no cover - optional during local testing
    AGORA_AVAILABLE = False
    AgoraService = AgoraServiceConfig = RTCConnection = RTCConnConfig = None  # type: ignore
    IAudioFrameObserver = AudioFrame = AudioPcmDataSender = None  # type: ignore
    IRTCLocalUserObserver = MediaNodeFactory = None  # type: ignore

from agora_token_builder import RtcTokenBuilder  # type: ignore

load_dotenv()

LOG = logging.getLogger("agora-voice-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
AGORA_APP_ID = os.environ.get("AGORA_APP_ID", "")
AGORA_APP_CERTIFICATE = os.environ.get("AGORA_APP_CERTIFICATE", "")
DEFAULT_CHANNEL = os.environ.get("AGORA_CHANNEL", "voice-agent-demo")
DEFAULT_BOT_UID = int(os.environ.get("AGORA_BOT_UID", "9999"))

VA_URL = "wss://agents.assemblyai.com/v1/ws"

# Agora delivers/accepts 16 kHz PCM frames easily; the Voice Agent API uses
# 24 kHz by default. We resample on each side of the bridge.
AGORA_RATE = 16_000
VA_RATE = 24_000

SYSTEM_PROMPT = (
    "You are a friendly voice assistant joining a live Agora call. "
    "Keep replies to one or two short sentences. Speak naturally."
)
GREETING = "Hi — I just joined the call. What would you like help with?"
VOICE = "ivy"


# ---------------------------------------------------------------------------
# Resampling helpers (polyphase, fast and high-quality)
# ---------------------------------------------------------------------------
def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    # Use small integer factors for resample_poly. 16k <-> 24k = 2/3.
    if from_rate == 16_000 and to_rate == 24_000:
        up, down = 3, 2
    elif from_rate == 24_000 and to_rate == 16_000:
        up, down = 2, 3
    else:
        from math import gcd
        g = gcd(from_rate, to_rate)
        up, down = to_rate // g, from_rate // g
    out = resample_poly(samples.astype(np.float32), up, down)
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# AssemblyAI Voice Agent API client
# ---------------------------------------------------------------------------
@dataclass
class AgentBridge:
    """Owns the WebSocket to AssemblyAI and exposes simple in/out queues."""

    ws: websockets.WebSocketClientProtocol
    inbound_audio: asyncio.Queue  # mic PCM16 @ 24 kHz from caller
    outbound_audio: asyncio.Queue  # reply PCM16 @ 24 kHz to publish
    session_ready: asyncio.Event
    stop: asyncio.Event

    @classmethod
    async def connect(cls) -> "AgentBridge":
        if not ASSEMBLYAI_API_KEY:
            raise RuntimeError("ASSEMBLYAI_API_KEY missing — set it in .env")
        headers = {"Authorization": f"Bearer {ASSEMBLYAI_API_KEY}"}
        ws = await websockets.connect(VA_URL, additional_headers=headers)
        bridge = cls(
            ws=ws,
            inbound_audio=asyncio.Queue(),
            outbound_audio=asyncio.Queue(),
            session_ready=asyncio.Event(),
            stop=asyncio.Event(),
        )
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "system_prompt": SYSTEM_PROMPT,
                "greeting": GREETING,
                "input": {"format": {"encoding": "audio/pcm"}},
                "output": {
                    "voice": VOICE,
                    "format": {"encoding": "audio/pcm"},
                },
            },
        }))
        return bridge

    async def run(self) -> None:
        await asyncio.gather(self._send_audio(), self._handle_events())

    async def _send_audio(self) -> None:
        await self.session_ready.wait()
        while not self.stop.is_set():
            chunk = await self.inbound_audio.get()
            if chunk is None:
                break
            await self.ws.send(json.dumps({
                "type": "input.audio",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _handle_events(self) -> None:
        async for raw in self.ws:
            event = json.loads(raw)
            t = event.get("type")

            if t == "session.ready":
                LOG.info("AssemblyAI session ready: %s", event.get("session_id"))
                self.session_ready.set()

            elif t == "transcript.user":
                LOG.info("User: %s", event.get("text", ""))

            elif t == "transcript.agent":
                tag = " (interrupted)" if event.get("interrupted") else ""
                LOG.info("Agent%s: %s", tag, event.get("text", ""))

            elif t == "reply.audio":
                pcm = base64.b64decode(event["data"])
                await self.outbound_audio.put(pcm)

            elif t == "reply.done":
                if event.get("status") == "interrupted":
                    # Drop any queued reply audio so we don't keep speaking
                    # over the user.
                    while not self.outbound_audio.empty():
                        try:
                            self.outbound_audio.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    LOG.info("Reply interrupted — flushed playback queue")

            elif t == "session.error":
                LOG.error("Voice Agent error %s: %s",
                          event.get("code"), event.get("message"))

    async def close(self) -> None:
        self.stop.set()
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Agora side of the bridge
# ---------------------------------------------------------------------------
class AgoraBridge:
    """Wraps the Agora RTC connection, the inbound audio observer, and the
    outbound PCM data sender. Exposes start() / push_outbound() / close().
    """

    def __init__(self, channel: str, uid: int, agent: AgentBridge,
                 loop: asyncio.AbstractEventLoop) -> None:
        if not AGORA_AVAILABLE:
            raise RuntimeError(
                "agora-python-server-sdk is not installed. "
                "pip install -r requirements.txt"
            )
        if not AGORA_APP_ID:
            raise RuntimeError("AGORA_APP_ID missing — set it in .env")

        self.channel = channel
        self.uid = uid
        self.agent = agent
        self.loop = loop

        # ----- AGORA_BRIDGE_TODO (init): if your SDK version names these
        # differently, this is the surface area you need to re-bind.
        cfg = AgoraServiceConfig()
        cfg.appid = AGORA_APP_ID
        self.service = AgoraService()
        self.service.initialize(cfg)

        self.connection = self.service.create_rtc_connection(RTCConnConfig(
            auto_subscribe_audio=1,
            auto_subscribe_video=0,
            client_role_type=1,            # 1 = BROADCASTER (can publish)
            channel_profile=1,             # 1 = LIVE_BROADCASTING
        ))

        self.media_factory: MediaNodeFactory = self.service.create_media_node_factory()
        self.pcm_sender: AudioPcmDataSender = self.media_factory.create_audio_pcm_data_sender()
        self.audio_track = self.service.create_custom_audio_track_pcm(self.pcm_sender)

        self.local_user = self.connection.get_local_user()
        self.local_user.set_playback_audio_frame_before_mixing_parameters(
            channels=1, sample_rate_hz=AGORA_RATE,
        )
        self.local_user.register_audio_frame_observer(self._make_observer())

    # -- inbound: caller -> AssemblyAI ---------------------------------------
    def _make_observer(self) -> "IAudioFrameObserver":
        agent = self.agent
        loop = self.loop

        class Observer(IAudioFrameObserver):  # type: ignore[misc]
            def on_playback_audio_frame_before_mixing(
                self, channel_id, uid, frame: "AudioFrame",
            ) -> int:
                # frame.buffer is 16-bit signed LE PCM at AGORA_RATE, mono.
                pcm16 = bytes(frame.buffer)
                pcm24 = resample_pcm16(pcm16, AGORA_RATE, VA_RATE)
                # Hand off to asyncio thread-safely.
                loop.call_soon_threadsafe(
                    agent.inbound_audio.put_nowait, pcm24
                )
                return 0

            # Other observer hooks must exist but we don't use them.
            def on_record_audio_frame(self, *_a, **_k):  # noqa: D401
                return 0

            def on_mixed_audio_frame(self, *_a, **_k):
                return 0

            def on_playback_audio_frame(self, *_a, **_k):
                return 0

            def on_ear_monitoring_audio_frame(self, *_a, **_k):
                return 0

        return Observer()

    # -- outbound: AssemblyAI -> caller --------------------------------------
    async def pump_outbound(self) -> None:
        """Pull reply.audio from the agent queue and push it into the
        Agora channel as PCM frames at 16 kHz.
        """
        while not self.agent.stop.is_set():
            pcm24 = await self.agent.outbound_audio.get()
            if pcm24 is None:
                break
            pcm16 = resample_pcm16(pcm24, VA_RATE, AGORA_RATE)
            # AGORA_BRIDGE_TODO (push): the exact signature varies by SDK
            # version. 2.2.x exposes send_audio_pcm_data(buffer, ts, samples,
            # bytes_per_sample, channels, sample_rate). If yours differs,
            # adjust here.
            samples_per_channel = len(pcm16) // 2  # 16-bit mono
            self.pcm_sender.send_audio_pcm_data(
                pcm16, 0, samples_per_channel, 2, 1, AGORA_RATE,
            )
            # Pace the writes to roughly real-time (10 ms blocks) so we
            # don't blast a whole reply into the buffer at once.
            await asyncio.sleep(samples_per_channel / AGORA_RATE)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        token = self._build_token()
        # AGORA_BRIDGE_TODO (connect): some SDK builds use connect(token, channel,
        # uid) while others use a config object; check your version.
        self.connection.connect(token, self.channel, str(self.uid))
        self.local_user.publish_audio(self.audio_track)
        LOG.info("Joined Agora channel '%s' as uid=%d", self.channel, self.uid)

    def _build_token(self) -> str:
        if not AGORA_APP_CERTIFICATE:
            # App Certificate disabled — Agora accepts an empty token.
            return ""
        import time
        expire_ts = int(time.time()) + 3600
        return RtcTokenBuilder.buildTokenWithUid(
            AGORA_APP_ID,
            AGORA_APP_CERTIFICATE,
            self.channel,
            self.uid,
            1,            # Role_Publisher
            expire_ts,
        )

    def close(self) -> None:
        try:
            self.local_user.unpublish_audio(self.audio_track)
            self.connection.disconnect()
            self.service.release()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Agora teardown error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main(channel: str, uid: int) -> None:
    loop = asyncio.get_running_loop()
    agent = await AgentBridge.connect()
    agora: Optional[AgoraBridge] = None

    try:
        agora = AgoraBridge(channel=channel, uid=uid, agent=agent, loop=loop)
        agora.start()

        stop_signal = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_signal.set)
            except NotImplementedError:
                pass  # Windows

        await asyncio.gather(
            agent.run(),
            agora.pump_outbound(),
            stop_signal.wait(),
        )
    finally:
        await agent.close()
        if agora is not None:
            agora.close()
        LOG.info("Bot stopped.")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Agora <-> Voice Agent API bot")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL,
                        help="Agora channel name to join")
    parser.add_argument("--uid", type=int, default=DEFAULT_BOT_UID,
                        help="UID the bot publishes under")
    args = parser.parse_args()

    if not AGORA_AVAILABLE:
        print(
            "agora-python-server-sdk is not installed. Run "
            "`pip install -r requirements.txt` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        asyncio.run(main(args.channel, args.uid))
    except KeyboardInterrupt:
        print("\nDisconnected.")


if __name__ == "__main__":
    cli()
