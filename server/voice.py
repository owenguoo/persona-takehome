"""The phone call: browser mic ⇄ WebRTC ⇄ Pipecat ⇄ OpenAI Realtime (speech-to-speech).

Transcripts from both sides land in the shared session history, so the text
agent knows what was said on the call (and the call knows the text thread).
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMRunFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    UserTurnMessageAddedMessage,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioNoiseReduction,
    InputAudioTranscription,
    SemanticTurnDetection,
    SessionProperties,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from . import calls, config, mail, prompts, text_agent
from .session import Session


def _describe(error: str, cfg: config.Settings) -> str | None:
    """Plain-words reason for errors that mean the call can't go on; None if it can."""
    if "invalid_api_key" in error or "401" in error:
        return "the OpenAI API key was rejected"
    if "model_not_found" in error or "does not exist" in error:
        return f"the voice model {cfg.realtime_model} isn't available on this key"
    if "insufficient_quota" in error:
        return "the OpenAI account is out of credit"
    if "ConnectionClosed" in error or "no close frame" in error:
        return "the voice connection dropped"
    return None


class CallTap(FrameProcessor):
    """Sits after the output transport: streams live captions + speaking state to the page."""

    def __init__(self, session: Session, generation: int):
        super().__init__()
        self._session = session
        self._gen = generation
        self._new_turn = True

    def _live(self) -> bool:
        return self._session.call.generation == self._gen

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and self._live():
            s = self._session
            if isinstance(frame, BotStartedSpeakingFrame):
                s.call.speaking = True
                await s.emit("speaking", on=True)
            elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)):
                self._new_turn = True
                s.call.speaking = False
                await s.emit("speaking", on=False)
            elif isinstance(frame, TTSTextFrame):
                if self._new_turn:
                    s.call.caption, self._new_turn = "", False
                s.call.caption += frame.text
                await s.emit("caption", role="agent", text=s.call.caption.strip())
        await self.push_frame(frame, direction)


async def run_call(session: Session, connection: SmallWebRTCConnection, generation: int) -> None:
    cfg = config.settings()
    failure: str | None = None
    try:
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        llm = OpenAIRealtimeLLMService(
            api_key=cfg.openai_api_key,
            settings=OpenAIRealtimeLLMService.Settings(
                model=cfg.realtime_model,
                system_instruction=prompts.voice_system(session),
                session_properties=SessionProperties(
                    audio=AudioConfiguration(
                        input=AudioInput(
                            transcription=InputAudioTranscription(),
                            turn_detection=SemanticTurnDetection(),
                            noise_reduction=InputAudioNoiseReduction(type="near_field"),
                        ),
                        output=AudioOutput(voice=cfg.voice),
                    ),
                ),
            ),
        )
        async def text_user(params: FunctionCallParams, message: str):
            """Text the user during the call, for anything easier to read than hear.

            Args:
                message: What to send, written the way you text: casual, lowercase is fine, short lines.
                    Put each separate text on its own line.
            """
            logger.info(f"[{session.id[:8]}] call  tool: text_user {message!r}")
            for i, bubble in enumerate(text_agent.split_bubbles(message, max_bubbles=6)):
                if i:
                    await asyncio.sleep(0.6)
                await session.add_message("agent", "text", bubble)
                session.remember("assistant", bubble, "text")
            await params.result_callback({"sent": True})

        async def send_gmail_link(params: FunctionCallParams):
            """Text the user a link card to connect their Gmail, once they've agreed."""
            if not mail.connected(session):
                await mail.send_link(session)
            await params.result_callback({"sent": True})

        async def inbox_overview(params: FunctionCallParams):
            """Their connected inbox at a glance: counts, top senders, the most recent emails."""
            await params.result_callback(mail.overview(session))

        async def read_inbox(params: FunctionCallParams):
            """Read every email in their inbox in full: for to-dos, what's urgent, deadlines."""
            await params.result_callback(mail.read_all(session))

        async def search_inbox(params: FunctionCallParams, query: str):
            """Find emails mentioning specific words (not for judgment questions like to-dos).

            Args:
                query: Words to look for in the sender, subject or body.
            """
            await params.result_callback(mail.search(session, query))

        async def read_email(params: FunctionCallParams, id: int):
            """Read one email in full.

            Args:
                id: The email id from inbox_overview or search_inbox.
            """
            await params.result_callback(mail.read(session, int(id)))

        context = LLMContext(
            [{"role": "developer", "content": prompts.voice_opening(session)}],
            # Only text_user: on a realtime call every tool call splits the agent's turn
            # around a pause, so anything that can wait (like saving the user's name)
            # is left to the text agent, which sees the call transcript afterwards.
            [text_user, send_gmail_link, inbox_overview, read_inbox, search_inbox, read_email],
        )
        user_agg, assistant_agg = LLMContextAggregatorPair(context)

        pipeline = Pipeline([
            transport.input(),
            user_agg,
            llm,
            transport.output(),
            CallTap(session, generation),
            assistant_agg,
        ])
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(),
            processor_unusable_policy=ProcessorUnusablePolicy.END,
        )
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)

        async def hangup():
            await runner.cancel()
            try:
                await connection.disconnect()
            except Exception:
                pass

        session.call.hangup = hangup

        async def inject(note: str):
            """Tell the live call something that just happened (e.g. Gmail connected).

            Pipecat's realtime service doesn't implement LLMMessagesAppendFrame yet, so this
            adds the conversation item directly and asks for a response, once the agent
            isn't mid-sentence (a response can't start while one is active).
            """
            for _ in range(50):
                if not session.call.speaking:
                    break
                await asyncio.sleep(0.2)
            if session.call.generation != generation or session.call.status != "active":
                return
            logger.info(f"[{session.id[:8]}] call  note: {note}")
            await llm.send_client_event(events.ConversationItemCreateEvent(item=events.ConversationItem(
                type="message", role="system", content=[events.ItemContent(type="input_text", text=note)])))
            await llm._create_response()

        session.call.inject = inject

        async def note(text: str):
            """Add context to the live call without prompting a reply."""
            if session.call.generation != generation or session.call.status != "active":
                return
            logger.info(f"[{session.id[:8]}] call  context: {text}")
            await llm.send_client_event(events.ConversationItemCreateEvent(item=events.ConversationItem(
                type="message", role="system", content=[events.ItemContent(type="input_text", text=text)])))

        session.call.note = note

        recent_errors: list[float] = []

        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(_worker, frame):
            nonlocal failure
            if failure:
                return
            now = time.monotonic()
            recent_errors[:] = [t for t in recent_errors if now - t < 5] + [now]
            reason = _describe(str(frame.error), cfg)
            if reason is None and len(recent_errors) >= 3:
                reason = "the voice service kept erroring"
            if reason:
                failure = reason
                logger.warning(f"[{session.id[:8]}] ending call: {reason} ({frame.error})")
                await runner.cancel()

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _client):
            await calls.connected(session)
            await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _client):
            await runner.cancel()

        # Captions are the agent's side only: the user's turn is finalized when the
        # agent starts replying, so showing it would flash over the agent's caption.
        @user_agg.event_handler("on_user_turn_message_added")
        async def on_user_turn(_agg, message: UserTurnMessageAddedMessage):
            logger.info(f"[{session.id[:8]}] call  user: {message.content}")
            session.remember("user", message.content, "voice")

        @assistant_agg.event_handler("on_assistant_turn_stopped")
        async def on_assistant_turn(_agg, message: AssistantTurnStoppedMessage):
            suffix = " (interrupted)" if message.interrupted else ""
            logger.info(f"[{session.id[:8]}] call agent: {message.content or '(nothing)'}{suffix}")
            if message.content:
                session.remember("assistant", message.content + suffix, "voice")

        await runner.run()
    except Exception:
        logger.exception(f"[{session.id[:8]}] voice call crashed")
        failure = failure or "the voice pipeline crashed"
    finally:
        await calls.finished(session, generation, error=failure)
