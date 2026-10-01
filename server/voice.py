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
    FunctionCallResultProperties,
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


GREET_AFTER_SECS = 1.3   # wait for a "hello?" before greeting
CLOSE_WAIT_SECS = 4      # after "anything else before i hang up?", this much quiet means no
MOVE_ON_SECS = 3.5       # stalled: said something that isn't a question, they're quiet → keep it moving
MAX_MOVE_ONS = 2         # …at most this many times in a row without them speaking
NUDGE_SECS = 8           # asked something, no answer → one gentle nudge
LINK_WAIT_SECS = 10      # waiting on the Gmail link → one "no rush"
SILENCE_CHECK_SECS = 15  # quiet this long → "still there?"; again → goodbye and hang up


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
                s.call.last_heard_at = time.time()
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
                            # "low": wait for a real end of turn, not a thinking pause ("hmm… probably email?")
                            turn_detection=SemanticTurnDetection(eagerness="low"),
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
                session.ob["asks"]["gmail"] += 1
                await mail.send_link(session)
            await params.result_callback({"sent": True})

        async def wrap_up(params: FunctionCallParams, kind: str, task: str = ""):
            """Start ending the call, right after you've said what happens next and asked if there's anything
            else (e.g. "i'll get that to you in a sec. anything else before i hang up?").

            Args:
                kind: "email" if you're going to work on their email, "other" for something else they asked
                    for, "none" if setup is simply done.
                task: What you'll be doing for them, in a few words (empty for "none").
            """
            session.ob["handoff"] = {"kind": kind if kind in ("email", "other") else "none", "task": task.strip()}
            session.call.closing = session.call.close_asked = True
            session.changed()
            logger.info(f"[{session.id[:8]}] call  tool: wrap_up {session.ob['handoff']}")
            # The closing question was said before this call: wait for their answer.
            await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))

        async def end_call(params: FunctionCallParams, kind: str = "", task: str = ""):
            """Hang up, right after your short goodbye.

            Args:
                kind: "email" if you're going off to work on their email, "other" if you're going off to work
                    on something else they asked for, "none" if setup is simply done. Say your goodbye
                    BEFORE calling this: nothing is said after it.
                task: What they asked you to do, in a few words (empty for "none").
            """
            if kind or not session.ob.get("handoff"):  # wrap_up may already have recorded it
                session.ob["handoff"] = {"kind": kind if kind in ("email", "other") else "none", "task": task.strip()}
            if not session.call.close_asked:
                # Never hang up abruptly: the first end_call becomes the closing question.
                session.call.closing = session.call.close_asked = True
                session.changed()
                logger.info(f"[{session.id[:8]}] call  tool: end_call too early, closing first")
                await params.result_callback(
                    {"not_yet": "Before hanging up, say what happens next if you haven't (\"i'll get that to you in a "
                                "sec\") and ask if there's anything else before you let them go. If they say no, "
                                "say a quick bye and call end_call again."},
                    properties=FunctionCallResultProperties(run_llm=True))
                return
            session.call.ending = True
            session.changed()
            logger.info(f"[{session.id[:8]}] call  tool: end_call {session.ob['handoff']}")
            # The goodbye was said before this call: don't prompt another turn.
            await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
            gen = generation

            async def hang_up_after_goodbye():
                # Let the goodbye finish: hang up once it's been quiet for a moment (max ~12s).
                quiet_since = time.monotonic()
                for _ in range(60):
                    await asyncio.sleep(0.2)
                    if session.call.speaking:
                        quiet_since = time.monotonic()
                    elif time.monotonic() - quiet_since > 1.2:
                        break
                if session.call.generation == gen:
                    await calls.hangup(session, "agent ended the call")

            asyncio.create_task(hang_up_after_goodbye())

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
            [text_user, send_gmail_link, inbox_overview, read_inbox, search_inbox, read_email, wrap_up, end_call],
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
            session.call.last_heard_at = time.time()
            asyncio.create_task(greet_unless_they_speak_first())
            asyncio.create_task(watch_silence())

        async def greet_unless_they_speak_first():
            # People answer with "hello?". Give them a beat; if they talk first, the reply to them
            # carries the greeting, so their opening words aren't talked over or lost.
            await asyncio.sleep(GREET_AFTER_SECS)
            if session.call.generation == generation and not session.call.user_speaking \
                    and not any(t.role == "user" and t.channel == "voice" and t.at >= (session.call.started_at or 0)
                                for t in session.history):
                await worker.queue_frames([LLMRunFrame()])

        async def watch_silence():
            """The call's version of the text-side loop: don't just wait for your turn.

            STALLED (said something that wasn't a question, they're quiet): keep it moving.
            WAITING on an answer (asked something, they're quiet): nudge gently, once.
            WAITING on the Gmail link: one easy "no rush".
            Long silence: "still there?", then a goodbye and hang up (follow up by text).
            """
            moved_on = 0          # proactive turns since they last spoke
            nudged = linked = checked_in = False
            while session.call.generation == generation and session.call.status == "active":
                await asyncio.sleep(0.5)
                c = session.call
                if c.ending or c.speaking or c.user_speaking or not c.agent_spoke:
                    continue
                quiet = time.time() - c.last_heard_at
                if user_spoke_since[0]:  # they talked: reset the nudges
                    user_spoke_since[0] = False
                    moved_on, nudged, linked, checked_in = 0, False, False, False
                if c.closing:  # "anything else before i hang up?": a short pause means no
                    if quiet >= CLOSE_WAIT_SECS:
                        c.closing = False
                        await inject("No answer, so they're all set. Say a quick, warm bye in a few words, "
                                     "then call end_call.")
                    continue
                link_out = (session.mail or {}).get("link_sent") and not mail.connected(session)
                if link_out:
                    if not linked and quiet >= LINK_WAIT_SECS:
                        linked = True
                        await inject("They're opening the Gmail link. Say one easy line like \"no rush, i'm here\".")
                        c.last_heard_at = time.time()
                    elif quiet >= SILENCE_CHECK_SECS * 2:
                        await say_goodbye_and_hang_up()
                        return
                    continue
                if not c.agent_asked and moved_on < MAX_MOVE_ONS and quiet >= MOVE_ON_SECS:
                    moved_on += 1
                    await inject("They haven't said anything and your last line wasn't a question. Just say the next "
                                 "thing in your flow, in one short line, as if continuing your thought. Never announce "
                                 "that you're moving on, and don't repeat yourself.")
                    c.last_heard_at = time.time()
                elif c.agent_asked and not nudged and quiet >= NUDGE_SECS:
                    nudged = True
                    await inject("They haven't answered your question. Nudge gently in one line: offer to skip it "
                                 "or suggest something (e.g. \"no rush, or i can suggest something\"). Never repeat "
                                 "the question word for word.")
                    c.last_heard_at = time.time()
                elif quiet >= SILENCE_CHECK_SECS:
                    if not checked_in:
                        checked_in = True
                        await inject("They've been quiet for a while. Check in, briefly and warmly (\"still there?\").")
                        c.last_heard_at = time.time()
                    else:
                        await say_goodbye_and_hang_up()
                        return

        async def say_goodbye_and_hang_up():
            await inject("Still nothing. Say a short, warm goodbye (you'll text them instead). Don't call any tool.")
            session.call.ending = True
            for _ in range(60):  # let the goodbye finish
                await asyncio.sleep(0.2)
                if not session.call.speaking and time.time() - session.call.last_heard_at > 1.2:
                    break
            if session.call.generation == generation:
                await calls.hangup(session, "silence")

        user_spoke_since = [False]  # set when they speak; the loop resets its nudges

        @user_agg.event_handler("on_user_turn_started")
        async def on_user_started(_agg, _strategy):
            session.call.closing = False  # they're saying something: hear them out
            session.call.user_speaking = True
            session.call.last_heard_at = time.time()
            user_spoke_since[0] = True

        @user_agg.event_handler("on_user_turn_stopped")
        async def on_user_stopped(_agg, _strategy, _message):
            session.call.user_speaking = False
            session.call.last_heard_at = time.time()

        @user_agg.event_handler("on_user_turn_message_added")
        async def on_user_turn(_agg, message: UserTurnMessageAddedMessage):
            logger.info(f"[{session.id[:8]}] call  user: {message.content}")
            session.remember("user", message.content, "voice")
            session.call.user_speaking = False
            session.call.last_heard_at = time.time()

        @assistant_agg.event_handler("on_assistant_turn_stopped")
        async def on_assistant_turn(_agg, message: AssistantTurnStoppedMessage):
            if message.content and message.content.strip():
                session.call.agent_spoke = True
                session.call.agent_asked = message.content.rstrip().endswith("?")
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
