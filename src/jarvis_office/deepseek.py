"""One async DeepSeek transport, bounded SSE/queues and explicitly confirmed RAM history.

Use ``async with client.turn(text) as turn: async for event in turn: ...``.
Events are raw user-facing ``delta`` or cleaned ``segment`` text, never reasoning/tools.
Only confirm text actually displayed/spoken. Generation alone never commits a turn.
No retries, health polling, provider routing, audio or V1 imports.
"""

import asyncio
import contextlib
import inspect
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from jarvis_office.config import Chat
from jarvis_office.pronounce import Pronounce, TextError

ENDPOINT = "https://api.deepseek.com/chat/completions"
SYSTEM = (
    "Tu es Jarvis. Réponds en français naturel, généralement en une à trois phrases. "
    "Commence par une information utile, sans préambule. N'utilise pas de Markdown, "
    "de code ou de longues URL. Tu n'as aucun outil : ne prétends jamais avoir ouvert "
    "une application, envoyé un message, consulté le web ou effectué une action."
)


class ChatError(Exception):
    """Constant reason only: never wrap an HTTP exception, body, URL or credential."""


class SSE:
    """Byte-oriented SSE lines: split UTF-8, CR/LF/CRLF, comments and multiline data."""

    def __init__(self) -> None:
        self.pending = b""
        self.data: list[str] = []
        self.event_bytes = self.wire_bytes = 0
        self.chunks = self.events = self.comments = 0
        self.first_line = True

    def feed(self, chunk: bytes) -> list[str]:
        self.wire_bytes += len(chunk)
        self.chunks += 1
        if len(chunk) > 65536 or self.wire_bytes > 1024 * 1024:
            raise ChatError("sse_wire_limit")
        self.pending += chunk
        events = []
        while match := re.search(rb"\r\n|\r|\n", self.pending):
            if match[0] == b"\r" and match.end() == len(self.pending):
                break  # Could be CRLF split between network buffers.
            raw = self.pending[: match.start()]
            self.pending = self.pending[match.end() :]
            if len(raw) > 16384:
                raise ChatError("sse_event_limit")
            try:
                line = raw.decode("utf-8")
            except UnicodeError:
                raise ChatError("sse_invalid_unicode") from None
            if self.first_line:
                line = line.removeprefix("\ufeff")
                self.first_line = False
            if not line:
                if self.data:
                    events.append("\n".join(self.data))
                    self.events += 1
                self.data = []
                self.event_bytes = 0
            elif line.startswith(":"):
                self.comments += 1
            else:
                field, sep, value = line.partition(":")
                if field == "data":
                    value = value.removeprefix(" ") if sep else ""
                    self.event_bytes += len(raw)
                    if self.event_bytes > 16384:
                        raise ChatError("sse_event_limit")
                    self.data.append(value)
        if len(self.pending) > 16384:
            raise ChatError("sse_event_limit")
        return events

    def finish(self) -> list[str]:
        # EOF disambiguates a final lone CR, but does not complete an unfinished event.
        return self.feed(b"\n") if self.pending.endswith(b"\r") else []


@dataclass(frozen=True)
class TextEvent:
    turn_id: str
    kind: Literal["delta", "segment"]
    text: str


class Turn:
    def __init__(self, owner: "DeepSeek", text: str, turn_id: str, context: str = "") -> None:
        self.owner, self.input, self.id = owner, text, turn_id
        self.context = context
        self.queue: asyncio.Queue[TextEvent] = asyncio.Queue(owner.settings.queue_events)
        self.generated = self.delivered_text = ""
        self.spoken_segments: list[str] = []  # Issued to consumer, not yet confirmed as spoken.
        self.error: str | None = None
        self.confirmed = False
        self.invalidated = False
        self.started = self.last_content = self.last_flush = time.perf_counter()
        self.metrics: dict[str, Any] = {
            "turn_id": turn_id,
            "date_utc": datetime.now(UTC).isoformat(),
            "status": "RUNNING",
            "requested_model": owner.settings.model,
            "returned_model": None,
            "endpoint": ENDPOINT,
            "thinking": {"type": "disabled"},
            "stream": True,
            "max_tokens": owner.settings.max_tokens,
            "requests": 0,
            "phase": "connect",
            "timeouts": {
                "connect_s": owner.settings.connect_timeout,
                "first_content_s": owner.settings.first_content_timeout,
                "idle_s": owner.settings.idle_timeout,
                "total_s": owner.settings.total_timeout,
                "http_read_s": owner.http.timeout.read,
            },
            "first_content_s": None,
            "first_segment_s": None,
            "text_end_s": None,
            "latency_scope": "text_only_not_voice",
            "segment_clock": "first_segment_s_is_queue_availability_not_audio_delivery",
            "generated_chars": 0,
            "delivered_chars": 0,
            "confirmed_chars": 0,
            "queue_peak": 0,
        }
        self.task: asyncio.Task[None] = asyncio.create_task(self._read())

    def _emit(self, kind: Literal["delta", "segment"], text: str) -> None:
        if self.error:
            raise ChatError(self.error)
        try:
            self.queue.put_nowait(TextEvent(self.id, kind, text))
        except asyncio.QueueFull:
            raise ChatError("consumer_backpressure") from None
        self.metrics["queue_peak"] = max(self.metrics["queue_peak"], self.queue.qsize())
        if kind == "segment" and self.metrics["first_segment_s"] is None:
            self.metrics["first_segment_s"] = time.perf_counter() - self.started

    def _chunk(self, data: str, segmenter: Pronounce) -> bool:
        if not data.strip():
            return False
        if data == "[DONE]":
            if self.metrics.get("finish_reason") != "stop":
                raise ChatError("missing_finish_reason")
            if self.metrics["first_content_s"] is None:
                raise ChatError("empty_response")
            if self.metrics["returned_model"] is None:
                raise ChatError("missing_returned_model")
            for segment in segmenter.finish():
                self._emit("segment", segment)
            if self.metrics["first_segment_s"] is None:
                raise ChatError("empty_pronounceable_response")
            self.metrics["text_end_s"] = time.perf_counter() - self.started
            return True
        try:
            chunk = json.loads(data)
        except (ValueError, RecursionError):
            raise ChatError("sse_invalid_json") from None
        if not isinstance(chunk, dict) or "error" in chunk:
            raise ChatError("invalid_stream_event")
        model = chunk.get("model")
        if model is not None:
            # The provider now returns this canonical name for the configured legacy Flash alias.
            if not isinstance(model, str) or not re.fullmatch(
                r"(?:deepseek-flash|deepseek-v4-flash(?:[-.][A-Za-z0-9]+)*)", model
            ):
                raise ChatError("unexpected_returned_model")
            if self.metrics["returned_model"] not in (None, model):
                raise ChatError("returned_model_changed_during_turn")
            self.metrics["returned_model"] = model
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.metrics["usage"] = {
                k: v
                for k, v in usage.items()
                if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and type(v) is int
                and 0 <= v <= 10_000_000
            }
        choices = chunk.get("choices", [])
        if choices == [] and isinstance(usage, dict):
            return False  # Also accept compatible usage-only frames, without speaking them.
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ChatError("invalid_choices")
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise ChatError("unexpected_choice_index")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ChatError("invalid_delta")
        if delta.get("reasoning_content") or delta.get("reasoning"):
            raise ChatError("unexpected_reasoning")
        if delta.get("tool_calls") or delta.get("function_call"):
            raise ChatError("unexpected_tool_call")
        if delta.get("role") not in (None, "assistant"):
            raise ChatError("unexpected_role")
        text = delta.get("content")
        if text is not None and not isinstance(text, str):
            raise ChatError("invalid_content")
        if text:
            text = "".join(
                c if c.isprintable() or c in "\n\t\r" else " " if c.isspace() else "" for c in text
            )
            if self.metrics.get("finish_reason"):
                raise ChatError("content_after_finish")
            if len(self.generated) + len(text) > self.owner.settings.output_chars:
                raise ChatError("response_size_limit")
            self.generated += text
            self.metrics["generated_chars"] = len(self.generated)
            if text.strip():
                self.last_content = time.perf_counter()
                if self.metrics["first_content_s"] is None:
                    self.metrics["first_content_s"] = self.last_content - self.started
                    self.metrics["phase"] = "streaming"
            self._emit("delta", text)
            for segment in segmenter.feed(text):
                self._emit("segment", segment)
                self.last_flush = time.perf_counter()
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason != "stop":
                raise ChatError(
                    {
                        "length": "output_token_limit",
                        "content_filter": "content_filtered",
                        "tool_calls": "unexpected_tool_call",
                    }.get(reason, "unexpected_finish_reason")
                )
            self.metrics["finish_reason"] = reason
        return False

    async def _read(self) -> None:
        settings = self.owner.settings
        response: httpx.Response | None = None
        pending: asyncio.Future[bytes] | None = None
        parser: SSE | None = None
        connections: dict[str, float] = {}

        async def trace(name: str, info: dict[str, Any]) -> None:
            # Never retain trace info: it can contain headers, URLs and exceptions.
            if name in {
                "connection.connect_tcp.started",
                "connection.connect_tcp.complete",
                "connection.start_tls.started",
                "connection.start_tls.complete",
            }:
                connections[name] = time.perf_counter() - self.started

        self.metrics["connection_trace"] = connections
        try:
            messages = self.owner._messages(self.input, self.context)
            request = self.owner.http.build_request(
                "POST",
                ENDPOINT,
                headers={
                    "Authorization": "Bearer " + self.owner._key,
                    "Accept": "text/event-stream",
                    "Accept-Encoding": "identity",
                },
                json={
                    "model": settings.model,
                    "messages": messages,
                    "stream": True,
                    "thinking": {"type": "disabled"},
                    "max_tokens": settings.max_tokens,
                    "stream_options": {"include_usage": True},
                },
                extensions={"trace": trace},
            )
            if self.owner.real_transport:
                from jarvis_office.config import ConfigError
                from jarvis_office.credentials import reserve_validation_request

                try:
                    reservation = await asyncio.to_thread(
                        self.owner.reserve_request or reserve_validation_request
                    )
                    if inspect.isawaitable(reservation):
                        reservation = await reservation
                    self.metrics[
                        "validation_attempt" if self.owner.reserve_request else "phase06_attempt"
                    ] = reservation
                except ConfigError as exc:
                    raise ChatError(exc.reason) from None
            self.metrics["requests"] = 1
            try:
                async with asyncio.timeout(
                    min(settings.first_content_timeout, settings.total_timeout)
                ):
                    response = await self.owner.http.send(request, stream=True)
            except TimeoutError:
                raise ChatError(
                    "total_timeout"
                    if settings.total_timeout <= settings.first_content_timeout
                    else "first_content_timeout"
                ) from None
            status = response.status_code
            self.metrics["http_status"] = status
            if status != 200:
                raise ChatError(
                    {
                        401: "unauthorized",
                        403: "forbidden",
                        402: "quota_exhausted",
                        429: "rate_limited",
                    }.get(
                        status,
                        "server_error"
                        if status >= 500
                        else "redirect_refused"
                        if 300 <= status < 400
                        else "http_error",
                    )
                )
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip()
                != "text/event-stream"
            ):
                raise ChatError("expected_event_stream")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ChatError("compressed_stream_refused")
            parser, segmenter = SSE(), Pronounce()
            self.metrics["phase"] = "first_content"
            iterator = response.aiter_raw().__aiter__()
            while True:
                now = time.perf_counter()
                if now >= self.started + settings.total_timeout:
                    raise ChatError("total_timeout")
                first = self.metrics["first_content_s"] is None
                activity_end = (
                    self.started + settings.first_content_timeout
                    if first
                    else self.last_content + settings.idle_timeout
                )
                if now >= activity_end:
                    raise ChatError("first_content_timeout" if first else "idle_timeout")
                if now >= self.last_flush + settings.segment_timeout:
                    for segment in segmenter.timed():
                        self._emit("segment", segment)
                    self.last_flush = now
                if pending is None:
                    pending = asyncio.ensure_future(anext(iterator))
                deadline = min(
                    self.started + settings.total_timeout,
                    activity_end,
                    self.last_flush + settings.segment_timeout,
                )
                ready, _ = await asyncio.wait(
                    {pending}, timeout=max(0, deadline - time.perf_counter())
                )
                if not ready:
                    continue  # Keep the same read alive: a segmentation timer must not cancel it.
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    for event in parser.finish():
                        if self._chunk(event, segmenter):
                            self.metrics["status"] = "PASS"
                            return
                    raise ChatError("truncated_stream") from None
                pending = None
                for event in parser.feed(chunk):
                    if self._chunk(event, segmenter):
                        self.metrics["status"] = "PASS"
                        return
                    await asyncio.sleep(0)  # Let a fast consumer run even for batched SSE frames.
        except asyncio.CancelledError:
            self.error = "cancelled"
        except (ChatError, TextError) as exc:
            self.error = str(exc)
        except httpx.ConnectTimeout:
            self.error = "connect_timeout"
        except httpx.TimeoutException as exc:
            self.error = "transport_timeout"
            self.metrics["transport_exception"] = type(exc).__name__
        except httpx.HTTPError as exc:
            self.error = "network_error"
            self.metrics["transport_exception"] = type(exc).__name__
        except Exception:
            self.error = "stream_operation_failed"
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if response is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(response.aclose(), timeout=settings.connect_timeout)
            if parser is not None:
                self.metrics["wire"] = {
                    "chunks": parser.chunks,
                    "bytes": parser.wire_bytes,
                    "events": parser.events,
                    "comments": parser.comments,
                }
            if self.error:
                self.metrics.update(
                    status="CANCELLED" if self.error == "cancelled" else "FAIL", reason=self.error
                )
                while not self.queue.empty():
                    self.queue.get_nowait()
            self.metrics["elapsed_s"] = time.perf_counter() - self.started

    def __aiter__(self) -> "Turn":
        return self

    async def __anext__(self) -> TextEvent:
        if self.error:
            raise ChatError(self.error)
        if self.task.done() and self.queue.empty():
            raise StopAsyncIteration
        get = asyncio.create_task(self.queue.get())
        try:
            await asyncio.wait({get, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if self.error:
                raise ChatError(self.error)
            if not get.done():
                if self.queue.empty():
                    raise StopAsyncIteration
                event = self.queue.get_nowait()
            else:
                event = get.result()
            if event.kind == "delta":
                self.delivered_text += event.text
                self.metrics["delivered_chars"] = len(self.delivered_text)
            else:
                self.spoken_segments.append(event.text)
            return event
        finally:
            get.cancel()
            await asyncio.gather(get, return_exceptions=True)

    async def cancel(self) -> None:
        if not self.task.done() or not self.queue.empty():
            self.error = "cancelled"  # Stop delivery before awaiting transport shutdown.
            self.metrics.update(status="CANCELLED", reason="cancelled")
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()

    def confirm(
        self, text: str, *, channel: Literal["displayed", "spoken"], complete: bool
    ) -> None:
        expected = self.delivered_text if channel == "displayed" else " ".join(self.spoken_segments)
        if (
            self.confirmed
            or self.invalidated
            or self.owner.active is not self
            or channel not in {"displayed", "spoken"}
            or not text.strip()
            or not expected.startswith(text)
        ):
            raise ChatError("invalid_turn_confirmation")
        if not self.task.done() or (
            complete
            and (self.metrics["status"] != "PASS" or text != expected or not self.queue.empty())
        ):
            raise ChatError("turn_not_complete")
        self.owner._commit(self.input, text, channel, complete)
        self.confirmed = True
        self.metrics.update(
            confirmed_chars=len(text), confirmation_channel=channel, confirmation_complete=complete
        )


class DeepSeek:
    def __init__(
        self,
        key: str,
        settings: Chat | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        reserve_request: Callable[[], int | Awaitable[int]] | None = None,
    ) -> None:
        settings = settings or Chat()
        if settings.model != "deepseek-v4-flash":
            raise ChatError("chat_requires_single_flash_model")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,512}", key):
            raise ChatError("invalid_key")
        self._key, self.settings = key, settings
        self.real_transport = transport is None
        self.reserve_request = reserve_request
        self.http = httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                settings.idle_timeout,
                connect=settings.connect_timeout,
                # A socket read may legitimately stay silent until the first-content deadline;
                # Turn._read enforces first-content and idle deadlines itself, always earlier.
                read=max(settings.first_content_timeout, settings.idle_timeout),
            ),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=transport,
        )
        self.history: list[tuple[str, str]] = []
        self.active: Turn | None = None
        self.closed = False

    def _messages(self, text: str, context: str = "") -> list[dict[str, str]]:
        while (
            self.history
            and len(SYSTEM)
            + len(text)
            + len(context)
            + sum(len(a) + len(b) for a, b in self.history)
            > self.settings.context_chars
        ):
            self.history.pop(0)
        if len(SYSTEM) + len(text) + len(context) > self.settings.context_chars:
            raise ChatError("context_size_limit")
        messages = [{"role": "system", "content": SYSTEM}]
        for question, answer in self.history:
            messages.extend(
                [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
            )
        if context:
            # Ephemeral evidence, not a system instruction or a retained conversation turn.
            messages.append({"role": "user", "content": context})
        return [*messages, {"role": "user", "content": text}]

    def _commit(self, question: str, answer: str, channel: str, complete: bool) -> None:
        if not complete:
            answer += (
                "\n[Réponse interrompue : seul cet extrait a été "
                + ("affiché" if channel == "displayed" else "prononcé")
                + ".]"
            )
        self.history.append((question, answer))
        while self.history and (
            len(self.history) > self.settings.history_turns
            or sum(len(a) + len(b) for a, b in self.history)
            > self.settings.context_chars - len(SYSTEM)
        ):
            self.history.pop(0)

    @contextlib.asynccontextmanager
    async def turn(
        self, text: str, *, turn_id: str | None = None, context: str = ""
    ) -> AsyncIterator[Turn]:
        if self.closed or self.active is not None:
            raise ChatError("client_closed" if self.closed else "one_active_turn_only")
        if not isinstance(text, str) or not text.strip() or len(text) > self.settings.input_chars:
            raise ChatError("invalid_input_text")
        if not isinstance(context, str) or len(context) > 4096:
            raise ChatError("invalid_ephemeral_context")
        identifier = str(uuid.uuid4()) if turn_id is None else turn_id
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
            raise ChatError("invalid_turn_id")
        turn = self.active = Turn(self, text, identifier, context)
        try:
            yield turn
        finally:
            await turn.cancel()
            turn.context = ""
            self.active = None

    async def reset(self) -> None:
        if self.active is not None:
            self.active.invalidated = True
            await self.active.cancel()
        self.history.clear()

    async def close(self) -> None:
        await self.reset()
        await self.http.aclose()
        self._key = ""
        self.closed = True
