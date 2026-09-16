import asyncio
import contextlib
import dataclasses
import io
import json
import re
import socket
import ssl
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from jarvis_office.cli import chat_command, main
from jarvis_office.config import Chat, ConfigError, load_config
from jarvis_office.credentials import import_key, load_key
from jarvis_office.deepseek import ENDPOINT, SSE, SYSTEM, ChatError, DeepSeek
from jarvis_office.pronounce import Pronounce, TextError

FAKE_KEY = "test-only-not-a-real-key"


def event(text=None, *, finish=None, delta=None, model="deepseek-v4-flash", **extra):
    value = {
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta if delta is not None else {"content": text},
                "finish_reason": finish,
            }
        ],
        **extra,
    }
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def complete(text="Un réseau local relie plusieurs appareils. Il permet de partager des données."):
    return [event(text), event(finish="stop"), b"data: [DONE]\n\n"]


class Wire(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for item in self.chunks:
            if isinstance(item, float):
                await asyncio.sleep(item)
            elif isinstance(item, Exception):
                raise item
            else:
                yield item

    async def aclose(self):
        self.closed = True


class ClosingWire(Wire):
    def __init__(self, outcome, *, defer_cancel=False):
        super().__init__(complete())
        self.outcome, self.defer_cancel = outcome, defer_cancel
        self.entered, self.cancelled, self.release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.close_task = None

    async def aclose(self):
        self.close_task = asyncio.current_task()
        self.entered.set()
        try:
            if self.outcome == "fail":
                raise RuntimeError("synthetic-close-failure")
            if self.outcome == "wait":
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.defer_cancel:
                await self.release.wait()
            raise
        self.closed = True


class ParserTests(unittest.TestCase):
    def test_sse_network_splits_unicode_comments_multiline_and_crlf(self):
        raw = (
            '\ufeff: keepalive\r\nid: x\r\ndata: {"texte":\r\n'
            'data: "réseau français"}\r\n\r\ndata: [DONE]\r\n\r\n'
        ).encode()
        for size in (1, 2, 7, 10000):
            parser = SSE()
            output = []
            for i in range(0, len(raw), size):
                output.extend(parser.feed(raw[i : i + size]))
            self.assertEqual(json.loads(output[0]), {"texte": "réseau français"})
            self.assertEqual(output[1], "[DONE]")
        parser = SSE()
        self.assertEqual(parser.feed(b"data: [DONE]\r\r"), [])
        self.assertEqual(parser.finish(), ["[DONE]"])

    def test_sse_memory_and_invalid_unicode(self):
        for raw, reason in (
            (b"x" * 17000, "sse_event_limit"),
            (b"x" * 70000, "sse_wire_limit"),
            (b"data: \xff\n\n", "sse_invalid_unicode"),
        ):
            with self.assertRaisesRegex(ChatError, reason):
                SSE().feed(raw)

    def test_segments_numbers_units_abbreviations_and_split_punctuation(self):
        segmenter = Pronounce()
        parts = [
            "M.",
            " Dupont transporte 3.",
            "14 kg, soit 12",
            " cm de plus.",
            ".",
            ". ",
            "Mme. Martin vérifie le camion.",
        ]
        emitted = []
        for value in parts:
            emitted.extend(segmenter.feed(value))
        emitted.extend(segmenter.finish())
        self.assertEqual(
            emitted,
            [
                "M. Dupont transporte 3.14 kg, soit 12 cm de plus...",
                "Mme. Martin vérifie le camion.",
            ],
        )
        self.assertEqual(segmenter.finish(), [])

    def test_timer_keeps_last_partial_word_and_number_unit_pair(self):
        segmenter = Pronounce()
        prefix = "Ce réseau permet de relier tous les appareils de votre "
        self.assertEqual(segmenter.feed(prefix + "bur"), [])
        self.assertEqual(segmenter.timed(), [prefix.strip()])
        self.assertEqual(segmenter.timed(), [])
        self.assertEqual(segmenter.feed("eau. "), ["bureau."])
        segmenter = Pronounce()
        segmenter.feed(prefix + "3,14 kilo")
        self.assertEqual(segmenter.timed(), [prefix.strip()])
        segmenter.feed("grammes.")
        self.assertEqual(segmenter.finish(), ["3,14 kilogrammes."])

    def test_markdown_code_links_and_limits(self):
        segmenter = Pronounce()
        text = (
            "**Bonjour**. Consultez [la page](https://exemple.invalid/a). "
            "```python\nsecret_code()\n``` La suite est simple."
        )
        output = []
        for char in text:
            output.extend(segmenter.feed(char))
        output.extend(segmenter.finish())
        self.assertEqual(" ".join(output), "Bonjour. Consultez la page. La suite est simple.")
        segmenter = Pronounce()
        segmenter.feed("Une explication utile. ```")
        for _ in range(20):
            segmenter.feed("x" * 200)
        self.assertLessEqual(len(segmenter.markdown.pending), 2)
        self.assertEqual(segmenter.finish(), [])
        for value in ("https://exemple.invalid/" + "x" * 600, "x" * 1200, "[" + "x" * 600):
            with self.assertRaises(TextError):
                Pronounce().feed(value)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="office chat ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "private/config/deepseek.env"
        self.source = self.root / "source.env"
        self.patch = patch("jarvis_office.credentials.secret_path", return_value=self.target)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_single_key_import_no_other_secret_no_shell_and_idempotence(self):
        self.source.write_text(
            'OTHER_SECRET=do-not-copy\nexport DEEPSEEK_API_KEY="' + FAKE_KEY + '" # note\n'
        )
        before = self.source.read_bytes()
        result = import_key(self.source)
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        self.assertEqual(load_key(), FAKE_KEY)
        self.assertNotIn("OTHER_SECRET", self.target.read_text())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.target.parent.stat().st_mode), 0o700)
        self.assertTrue(import_key(self.source)["reused"])
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_invalid_ambiguous_permissions_and_links_fail_redacted(self):
        for value in (
            "OTHER_SECRET=private",
            "DEEPSEEK_API_KEY=$(whoami)",
            "DEEPSEEK_API_KEY=abcdefgh\nDEEPSEEK_API_KEY=ijklmnop",
            'DEEPSEEK_API_KEY="unterminated',
        ):
            self.source.write_text(value)
            with self.assertRaises(ConfigError) as caught:
                import_key(self.source)
            self.assertNotIn(value, str(caught.exception))
        self.source.write_text("DEEPSEEK_API_KEY=" + FAKE_KEY)
        import_key(self.source)
        self.target.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "0600"):
            load_key()
        self.target.unlink()
        self.target.symlink_to(self.source)
        with self.assertRaisesRegex(ConfigError, "link_refused"):
            load_key()

    def test_chat_config_and_missing_key_cli_without_network(self):
        config = self.root / "config.toml"
        for raw in (
            'model="deepseek-v4-pro"',
            'base_url="https://private.invalid/key"',
            "max_tokens=257",
            "idle_timeout=nan",
            "history_turns=true",
        ):
            config.write_text("[chat]\n" + raw)
            with self.assertRaises(ConfigError):
                load_config(config)
        config.write_text("[chat]\nmax_tokens=256")
        self.assertEqual(load_config(config).chat.max_tokens, 256)
        stream = io.StringIO()
        with (
            contextlib.redirect_stdout(stream),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            code = main(["chat", "--text", "Texte privé synthétique", "--config", str(config)])
        self.assertEqual(code, 3)
        self.assertNotIn("Texte privé", stream.getvalue())
        self.assertNotIn(str(config), stream.getvalue())


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_trace_retains_only_four_timestamps(self):
        async def handler(request):
            callback = request.extensions["trace"]
            for name in (
                "connection.connect_tcp.started",
                "connection.connect_tcp.complete",
                "connection.start_tls.started",
                "connection.start_tls.complete",
                "http11.send_request_headers.started",
            ):
                await callback(name, {"Authorization": "private_trace_secret"})
            return httpx.Response(401)

        client = DeepSeek("test_key_only", transport=httpx.MockTransport(handler))
        try:
            async with client.turn("Question synthétique.") as turn:
                with self.assertRaises(ChatError):
                    async for _ in turn:
                        pass
            self.assertEqual(len(turn.metrics["connection_trace"]), 4)
            self.assertNotIn("private_trace_secret", json.dumps(turn.metrics))
        finally:
            await client.close()

    async def test_tls_verification_enabled_without_proxy_environment(self):
        client = DeepSeek(FAKE_KEY)
        self.clients.append(client)
        context = client.http._transport._pool._ssl_context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertFalse(client.http.trust_env)

    async def test_connect_timeout_and_header_first_content_deadline(self):
        def timeout(request):
            raise httpx.ConnectTimeout("private credential " + FAKE_KEY)

        with self.assertRaisesRegex(ChatError, "connect_timeout"):
            await self.collect(self.client(handler=timeout))

        async def slow(request):
            await asyncio.sleep(0.1)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Wire(complete())
            )

        client = DeepSeek(
            FAKE_KEY,
            dataclasses.replace(Chat(), first_content_timeout=0.02),
            transport=httpx.MockTransport(slow),
        )
        self.clients.append(client)
        with self.assertRaisesRegex(ChatError, "first_content_timeout"):
            await self.collect(client)

    async def test_cli_displays_progress_and_redacted_metadata_without_audio(self):
        client = self.client([event("Première phrase utile. "), 0.02, *complete("La suite.")])
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch("jarvis_office.credentials.load_key", return_value=FAKE_KEY),
            patch("jarvis_office.deepseek.DeepSeek", return_value=client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = await chat_command(Chat(), "Question synthétique privée", None)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), "Première phrase utile. La suite.")
        metrics = json.loads(errors.getvalue())
        self.assertEqual(metrics["confirmation_channel"], "displayed")
        for private in (FAKE_KEY, "Question synthétique privée", "Première phrase utile"):
            self.assertNotIn(private, errors.getvalue())

    async def asyncSetUp(self):
        self.clients = []
        self.requests = []
        self.wires = []
        self.no_network = patch(
            "socket.socket.connect", side_effect=AssertionError("real network forbidden")
        )
        self.no_network.start()

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
            self.assertTrue(client.http.is_closed)
        self.no_network.stop()

    def client(self, chunks=None, *, settings=None, status=200, headers=None, handler=None):
        def respond(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            wire = Wire(chunks if chunks is not None else complete())
            self.wires.append(wire)
            return httpx.Response(
                status, headers=headers or {"content-type": "text/event-stream"}, stream=wire
            )

        client = DeepSeek(FAKE_KEY, settings, transport=httpx.MockTransport(respond))
        self.clients.append(client)
        return client

    async def collect(self, client):
        events = []
        async with client.turn("Explique un réseau local.") as turn:
            async for e in turn:
                events.append(e)
        return turn, events

    async def test_canonical_flash_response_preserves_configured_request(self):
        client = self.client(
            [
                event("Présent.", model="deepseek-flash"),
                event(finish="stop", model="deepseek-flash"),
                b"data: [DONE]\n\n",
            ]
        )
        turn, events = await self.collect(client)
        self.assertEqual(turn.metrics["status"], "PASS")
        self.assertEqual(turn.metrics["returned_model"], "deepseek-flash")
        self.assertTrue(any(e.kind == "segment" for e in events))
        self.assertEqual(json.loads(self.requests[0].content)["model"], "deepseek-v4-flash")
        self.assertEqual(len(self.requests), 1)

    async def test_exact_contract_model_metrics_history_requires_confirmation(self):
        client = self.client()
        async with client.turn("Une question synthétique", turn_id="turn-1") as turn:
            events = [e async for e in turn]
            self.assertEqual(client.history, [])
            turn.confirm(turn.delivered_text, channel="displayed", complete=True)
        body = json.loads(self.requests[0].content)
        self.assertEqual(str(self.requests[0].url), ENDPOINT)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["max_tokens"], 256)
        self.assertTrue(body["stream"])
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("tools", body)
        self.assertNotIn(FAKE_KEY, json.dumps(body))
        self.assertFalse(client.http.follow_redirects)
        self.assertFalse(client.http.trust_env)
        self.assertEqual(turn.metrics["status"], "PASS")
        self.assertEqual(turn.metrics["returned_model"], "deepseek-v4-flash")
        self.assertLessEqual(turn.metrics["first_content_s"], turn.metrics["first_segment_s"])
        self.assertLessEqual(turn.metrics["first_segment_s"], turn.metrics["text_end_s"])
        self.assertTrue(all(e.turn_id == "turn-1" for e in events))
        self.assertEqual(len(client.history), 1)
        self.assertTrue(self.wires[0].closed)

    async def test_slow_stream_first_segment_before_end_and_no_half_word(self):
        prefix = "Un réseau local relie simplement plusieurs appareils dans votre "
        client = self.client(
            [event(prefix + "bur"), 0.09, event("eau."), event(finish="stop"), b"data: [DONE]\n\n"],
            settings=dataclasses.replace(Chat(), segment_timeout=0.02),
        )
        async with client.turn("Question synthétique") as turn:
            segments = []
            async for e in turn:
                if e.kind == "segment":
                    segments.append(e.text)
                    if len(segments) == 1:
                        self.assertFalse(turn.task.done())
                        self.assertEqual(e.text, prefix.strip())
            self.assertEqual(" ".join(segments), prefix + "bureau.")
            self.assertLess(turn.metrics["first_segment_s"], turn.metrics["text_end_s"])

    async def test_usage_without_choices_role_empty_deltas_unicode_byte_splits(self):
        raw = (
            b": keepalive\n\n"
            + event(delta={"role": "assistant"})
            + event("Un réseau français de 3,14\u00a0m.")
            + event(finish="stop")
            + b'data: {"usage":{"completion_tokens":8}}\n\ndata: [DONE]\n\n'
        )
        client = self.client([raw[i : i + 1] for i in range(len(raw))])
        turn, events = await self.collect(client)
        self.assertEqual(
            "".join(e.text for e in events if e.kind == "delta"), "Un réseau français de 3,14 m."
        )
        self.assertEqual(turn.metrics["usage"], {"completion_tokens": 8})

    async def test_http_statuses_are_distinct_no_body_or_credential_leak_no_retry(self):
        for status, reason in (
            (401, "unauthorized"),
            (403, "forbidden"),
            (402, "quota_exhausted"),
            (429, "rate_limited"),
            (500, "server_error"),
            (503, "server_error"),
            (307, "redirect_refused"),
        ):
            before = len(self.requests)
            client = self.client(
                [FAKE_KEY.encode()],
                status=status,
                headers={"location": "https://evil.invalid/secret", "content-type": "text/plain"},
            )
            with self.subTest(status=status), self.assertRaisesRegex(ChatError, reason):
                await self.collect(client)
            self.assertEqual(len(self.requests) - before, 1)

    async def test_protocol_errors_never_speak_reasoning_or_tools(self):
        cases = [
            ([b"data: broken\n\n"], "sse_invalid_json"),
            (
                [event(delta={"reasoning_content": "private thought", "content": "Do not speak"})],
                "unexpected_reasoning",
            ),
            ([event(delta={"tool_calls": [{"secret": FAKE_KEY}]})], "unexpected_tool_call"),
            ([event("Bonjour", model="deepseek-v4-pro")], "unexpected_returned_model"),
            ([event("Bonjour", model="deepseek-flash-pro")], "unexpected_returned_model"),
            (
                [
                    event("Bonjour", model="deepseek-flash"),
                    event("suite", model="deepseek-v4-flash"),
                ],
                "returned_model_changed_during_turn",
            ),
            ([event(finish="stop"), b"data: [DONE]\n\n"], "empty_response"),
            ([event("Fragment", finish="length")], "output_token_limit"),
            ([event("Une réponse coupée")], "truncated_stream"),
            ([event("Réponse."), b"data: [DONE]\n\n"], "missing_finish_reason"),
        ]
        for chunks, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ChatError, reason):
                await self.collect(self.client(chunks))

    async def test_timeouts_keepalive_cannot_extend_first_idle_or_total(self):
        for chunks, settings, reason in (
            (
                [b": keepalive\n\n", 0.1],
                dataclasses.replace(Chat(), first_content_timeout=0.02),
                "first_content_timeout",
            ),
            (
                [event("Bonjour. "), b": keepalive\n\n", 0.1],
                dataclasses.replace(Chat(), idle_timeout=0.02),
                "idle_timeout",
            ),
            (
                [event("Bonjour. "), 0.1],
                dataclasses.replace(Chat(), total_timeout=0.02),
                "total_timeout",
            ),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ChatError, reason):
                await self.collect(self.client(chunks, settings=settings))

    async def test_exception_redaction_and_no_retry_after_content(self):
        client = self.client(
            [
                event("Début de réponse. "),
                0.01,
                httpx.ReadError(
                    "Authorization Bearer " + FAKE_KEY + " https://private.invalid/secret"
                ),
            ]
        )
        with self.assertRaisesRegex(ChatError, "network_error") as caught:
            await self.collect(client)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(client.history, [])
        self.assertTrue(self.wires[0].closed)

    async def test_compression_refused_before_decoding_and_reset_invalidates_confirmation(self):
        client = self.client(
            [b"not decoded"],
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
        )
        with self.assertRaisesRegex(ChatError, "compressed_stream_refused"):
            await self.collect(client)
        client = self.client()
        async with client.turn("Question") as turn:
            async for _ in turn:
                pass
            await client.reset()
            with self.assertRaisesRegex(ChatError, "invalid_turn_confirmation"):
                turn.confirm(turn.delivered_text, channel="displayed", complete=True)
        self.assertEqual(client.history, [])

    async def test_cancel_midstream_second_turn_and_partial_spoken_confirmation(self):
        client = self.client(
            [
                event("Première phrase utile. "),
                0.2,
                event("À annuler."),
                event(finish="stop"),
                b"data: [DONE]\n\n",
            ]
        )
        async with client.turn("Première demande") as turn:
            async for e in turn:
                if e.kind == "segment":
                    await turn.cancel()
                    turn.confirm(e.text, channel="spoken", complete=False)
                    break
            with self.assertRaisesRegex(ChatError, "cancelled"):
                await anext(turn)
            self.assertEqual(turn.metrics["status"], "CANCELLED")
        self.assertIn("Réponse interrompue", client.history[0][1])
        async with client.turn("Deuxième demande") as second:
            items = [e async for e in second]
            self.assertTrue(all(e.turn_id == second.id for e in items))
            self.assertNotEqual(second.id, turn.id)
            self.assertEqual(second.metrics["status"], "PASS")
        self.assertEqual(len(self.requests), 2)

    async def test_one_active_turn_reset_and_unconfirmed_failure_not_history(self):
        client = self.client([0.2, *complete()])
        async with client.turn("Question") as turn:
            with self.assertRaisesRegex(ChatError, "one_active"):
                async with client.turn("Autre"):
                    pass
            await client.reset()
            self.assertEqual(client.history, [])
            self.assertEqual(turn.metrics["status"], "CANCELLED")
        await client.close()
        with self.assertRaisesRegex(ChatError, "client_closed"):
            async with client.turn("Après fermeture"):
                pass

    async def test_bounded_queue_slow_consumer_output_and_context_limits(self):
        client = self.client(
            [event("mot ") for _ in range(20)], settings=dataclasses.replace(Chat(), queue_events=2)
        )
        async with client.turn("Question") as turn:
            await turn.task
            self.assertEqual(turn.error, "consumer_backpressure")
            self.assertLessEqual(turn.metrics["queue_peak"], 2)
            self.assertTrue(turn.queue.empty())
        client = self.client(
            [event("x" * 300)], settings=dataclasses.replace(Chat(), output_chars=100)
        )
        with self.assertRaisesRegex(ChatError, "response_size_limit"):
            await self.collect(client)
        client = self.client(
            settings=dataclasses.replace(Chat(), history_turns=2, context_chars=1000)
        )
        for i in range(4):
            async with client.turn("Question " + str(i)) as turn:
                async for _ in turn:
                    pass
                turn.confirm(turn.delivered_text, channel="displayed", complete=True)
        self.assertEqual(len(client.history), 2)
        self.assertLessEqual(sum(len(a) + len(b) for a, b in client.history) + len(SYSTEM), 1000)
        await client.reset()
        self.assertEqual(client.history, [])

    async def test_context_metadata_describes_intercepted_payload_without_content(self):
        from jarvis_office.echo.context import PassiveContextBuffer

        first, second = "Fait synthétique confidentiel alpha.", "Autre fait synthétique beta."
        now = [0.0]
        buffer = PassiveContextBuffer(seconds=30, clock=lambda: now[0])
        client = self.client()
        for operation, chars, limit, expected, reason in (
            ("empty", 3500, 20, 0, "EMPTY"),
            ("add", 3500, 20, 2, "ALL_SELECTED"),
            ("keep", len(second), 20, 1, "CHAR_LIMIT"),
            ("keep", 3500, 1, 1, "UTTERANCE_LIMIT"),
            ("keep", 1, 20, 0, "CHAR_LIMIT"),
            ("expire", 3500, 20, 0, "EXPIRED"),
            ("clear", 3500, 20, 0, "EMPTY"),
        ):
            if operation == "add":
                buffer.add(first)
                buffer.add(second)
            elif operation == "expire":
                now[0] = 31.0
            elif operation == "clear":
                buffer.add(first)
                buffer.clear()
            context, selection = buffer.select(max_chars=chars, max_utterances=limit)
            selection["ignored_untrusted_field"] = first
            async with client.turn(
                "Question de fixture", context=context, context_metadata=selection
            ) as turn:
                async for _ in turn:
                    pass
            payload = json.loads(self.requests[-1].content)
            proof = turn.metrics["request_context"]
            self.assertEqual(proof["selection_reason"], reason)
            self.assertEqual(proof["selected_entries"], expected)
            self.assertEqual(proof["incorporated_entries"], expected)
            self.assertEqual(proof["payload_includes_passive_context"], bool(expected))
            self.assertEqual(proof["incorporated_payload_chars"], len(context))
            self.assertEqual(
                proof["payload_chars"], sum(len(m["content"]) for m in payload["messages"])
            )
            self.assertEqual(len(payload["messages"]), 3 if expected else 2)
            if expected:
                self.assertEqual(payload["messages"][-2], {"role": "user", "content": context})
                self.assertEqual(
                    proof["selected_chars"],
                    len(first) + len(second) if expected == 2 else len(second),
                )
            self.assertEqual(len(proof["selected_entry_ids"]), expected)
            exported = json.dumps(proof)
            for text in (first, second, "Question de fixture", FAKE_KEY):
                self.assertNotIn(text, exported)
            self.assertLess(len(exported), 4096)

        for _ in range(25):
            buffer.add(first)
        context, selection = buffer.select(max_chars=3000, max_utterances=25)
        async with client.turn("Question", context=context, context_metadata=selection) as turn:
            async for _ in turn:
                pass
        self.assertEqual(turn.metrics["request_context"]["incorporated_entries"], 25)
        self.assertEqual(len(turn.metrics["request_context"]["selected_entry_ids"]), 20)
        self.assertEqual(turn.metrics["request_context"]["entry_ids_truncated"], 5)

    async def test_prepared_payload_is_not_a_network_attempt_when_reservation_is_refused(self):
        client = self.client()
        client.real_transport = True  # Still MockTransport; only the injected reservation runs.
        client.reserve_request = Mock(side_effect=ConfigError("test_budget_exhausted"))
        async with client.turn("Question de fixture") as turn:
            await turn.task
        self.assertEqual(turn.error, "test_budget_exhausted")
        self.assertTrue(turn.metrics["request_context"]["payload_constructed"])
        self.assertIsNotNone(turn.metrics["timeline"]["request_prepared_s"])
        self.assertIsNone(turn.metrics["timeline"]["network_started_s"])
        self.assertEqual(turn.metrics["requests"], 0)
        self.assertEqual(self.requests, [])

    async def test_request_build_encoding_failure_retains_safe_unprepared_proof(self):
        client = self.client()
        metadata = {
            "server_epoch": "server-fixture",
            "selected_entries": 1,
            "selected_chars": 7,
            "selection_reason": "ALL_SELECTED",
            "ignored": "private-build-fixture",
        }
        with patch.object(client.http, "build_request", wraps=client.http.build_request) as build:
            async with client.turn(
                "private-build-fixture\ud800", context="context fixture", context_metadata=metadata
            ) as turn:
                with self.assertRaisesRegex(ChatError, "stream_operation_failed"):
                    async for _ in turn:
                        pass
        build.assert_called_once()
        self.assertEqual(self.requests, [])
        self.assertEqual(turn.metrics["requests"], 0)
        self.assertEqual(turn.metrics["status"], "FAIL")
        proof = turn.metrics["request_context"]
        self.assertFalse(proof["payload_constructed"])
        self.assertFalse(proof["payload_includes_passive_context"])
        self.assertEqual(proof["incorporation_reason"], "NOT_PREPARED")
        self.assertEqual(proof["selected_entries"], 1)
        self.assertEqual(proof["selected_chars"], 7)
        self.assertEqual(proof["server_epoch"], "server-fixture")
        self.assertEqual(proof["incorporated_entries"], 0)
        self.assertNotIn("payload_chars", proof)
        for value in ("private-build-fixture", "context fixture", "\\ud800"):
            self.assertNotIn(value, json.dumps(proof))
        timeline = turn.metrics["timeline"]
        self.assertIsNone(timeline["request_prepared_s"])
        self.assertIsNone(timeline["network_started_s"])
        self.assertIsNone(timeline["response_closed_s"])
        self.assertLessEqual(timeline["stop_s"], timeline["finalized_s"])
        self.assertTrue(turn.task.done())

    async def test_immediate_turn_exit_finalizes_without_preparing_request(self):
        client = self.client()
        with patch.object(client.http, "build_request", wraps=client.http.build_request) as build:
            async with client.turn(
                "immediate-exit-fixture",
                context="unused context",
                context_metadata={"selected_entries": 1, "conversation_session": "session-fixture"},
            ) as turn:
                pass
        build.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertEqual(turn.metrics["requests"], 0)
        self.assertEqual(turn.metrics["status"], "CANCELLED")
        self.assertEqual(turn.error, "cancelled")
        proof = turn.metrics["request_context"]
        self.assertEqual(proof["turn_id"], turn.id)
        self.assertEqual(proof["selected_entries"], 1)
        self.assertEqual(proof["conversation_session"], "session-fixture")
        self.assertFalse(proof["payload_constructed"])
        self.assertFalse(proof["payload_includes_passive_context"])
        self.assertEqual(proof["incorporation_reason"], "NOT_PREPARED")
        self.assertNotIn("immediate-exit-fixture", json.dumps(proof))
        timeline = turn.metrics["timeline"]
        self.assertIsNone(timeline["network_started_s"])
        self.assertIsNone(timeline["request_prepared_s"])
        self.assertIsNone(timeline["response_closed_s"])
        self.assertLessEqual(timeline["stop_s"], timeline["finalized_s"])
        self.assertFalse(timeline["read_pending_at_stop"])
        self.assertTrue(turn.task.cancelled())
        self.assertTrue(turn.queue.empty())
        self.assertIsNone(client.active)

    async def test_response_close_success_and_failure_have_distinct_final_evidence(self):
        for outcome in ("success", "fail"):
            with self.subTest(outcome=outcome):
                wire = ClosingWire(outcome)
                client = self.client(
                    handler=lambda request, wire=wire: httpx.Response(
                        200, headers={"content-type": "text/event-stream"}, stream=wire
                    )
                )
                async with client.turn("synthetic question") as turn:
                    async for _ in turn:
                        pass
                    await turn.task
                timeline = turn.metrics["timeline"]
                self.assertTrue(wire.entered.is_set())
                self.assertTrue(wire.close_task.done())
                self.assertLessEqual(timeline["stop_s"], timeline["finalized_s"])
                self.assertEqual(timeline["response_closed_s"] is not None, outcome == "success")
                self.assertEqual(wire.closed, outcome == "success")
                if outcome == "success":
                    self.assertLessEqual(timeline["stop_s"], timeline["response_closed_s"])
                    self.assertLessEqual(timeline["response_closed_s"], timeline["finalized_s"])

    async def test_cancel_during_response_close_finalizes_joined_turn(self):
        wire = ClosingWire("wait")
        client = self.client(
            handler=lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=wire
            )
        )
        async with client.turn("synthetic question") as turn:
            await asyncio.wait_for(wire.entered.wait(), 1)
            await asyncio.wait_for(turn.cancel(), 1)
            self.assertTrue(turn.task.done())
            self.assertTrue(wire.close_task.done())
            self.assertTrue(turn.queue.empty())
            self.assertEqual(turn.error, "cancelled")
        self.assertIsNone(client.active)
        self.assertFalse(wire.closed)
        self.assertIsNone(turn.metrics["timeline"]["response_closed_s"])
        self.assertLessEqual(
            turn.metrics["timeline"]["stop_s"], turn.metrics["timeline"]["finalized_s"]
        )
        self.assertEqual(len(self.requests), 1)

    async def test_cancel_caller_and_concurrent_cancel_still_join_response_cleanup(self):
        wire = ClosingWire("wait", defer_cancel=True)
        client = self.client(
            handler=lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=wire
            )
        )
        async with client.turn("synthetic question") as turn:
            await asyncio.wait_for(wire.entered.wait(), 1)
            first = asyncio.create_task(turn.cancel())
            await asyncio.wait_for(wire.cancelled.wait(), 1)
            first.cancel()
            second = asyncio.create_task(turn.cancel())
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.assertFalse(wire.close_task.done())
            self.assertIs(client.active, turn)
            wire.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 1
            )
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertIsNone(results[1])
            self.assertTrue(turn.task.done())
            self.assertTrue(wire.close_task.done())
        self.assertIsNone(client.active)
        self.assertFalse(wire.closed)
        self.assertIsNone(turn.metrics["timeline"]["response_closed_s"])
        self.assertIsNotNone(turn.metrics["timeline"]["finalized_s"])
        self.assertTrue(turn.queue.empty())
        self.assertEqual(len(self.requests), 1)


SCALED = dataclasses.replace(
    Chat(), connect_timeout=0.6, first_content_timeout=1.2, idle_timeout=0.6, total_timeout=3.6
)  # Production 10/20/10/60 s divided by ~16.7: same order, first content = 2 × idle = total / 6.


class LoopbackServer:
    """Real HTTP/1.1 loopback origin: HTTPX socket timeouts stay effective, unlike MockTransport."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)  # One per connection: bytes to send or seconds of silence.
        self.connections = self.closed_by_client = 0
        self.tasks = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()

    async def serve(self, reader, writer):
        self.tasks.add(asyncio.current_task())
        self.connections += 1
        head = await reader.readuntil(b"\r\n\r\n")
        await reader.readexactly(int(re.search(rb"(?i)content-length: *(\d+)", head)[1]))
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n"
        )
        eof = asyncio.ensure_future(reader.read())  # Resolves once the client closes its side.
        try:
            for step in self.scripts.pop(0):
                if isinstance(step, float):
                    done, _ = await asyncio.wait({eof}, timeout=step)
                    if done:
                        break
                else:
                    writer.write(step)
                    await writer.drain()
        except OSError:
            pass
        finally:
            if eof.done() and not eof.cancelled():
                self.closed_by_client += 1
            eof.cancel()
            writer.close()


class LoopbackTransport(httpx.AsyncHTTPTransport):
    """Real socket transport aimed at the loopback origin: the fixed endpoint is never resolved."""

    def __init__(self, port):
        super().__init__()
        self.port = port

    async def handle_async_request(self, request):
        assert request.url.host == "api.deepseek.com"
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return await super().handle_async_request(request)


class LoopbackTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Timeout contract over a real loopback HTTP origin, with the production timeout ratios."""

    async def asyncSetUp(self):
        connect = socket.socket.connect

        def loopback_only(sock, address):
            if address[0] != "127.0.0.1":
                raise AssertionError("real network forbidden")
            return connect(sock, address)

        self.guard = patch("socket.socket.connect", new=loopback_only)
        self.guard.start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
            self.assertTrue(client.http.is_closed)
        self.guard.stop()
        self.assertEqual(asyncio.all_tasks() - {asyncio.current_task()}, set())

    def client(self, server):
        client = DeepSeek(FAKE_KEY, SCALED, transport=LoopbackTransport(server.port))
        self.clients.append(client)
        return client

    async def collect(self, client):
        events, error = [], None
        async with client.turn("Explique un réseau local.") as turn:
            try:
                async for e in turn:
                    events.append(e)
            except ChatError as exc:
                error = str(exc)
        return turn, events, error

    async def until(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not met within 2 s")

    async def test_a_stall_after_headers_within_first_content_deadline_streams(self):
        # Incident shape: 200 text/event-stream at once, then silence longer than idle_timeout
        # but shorter than first_content_timeout, then a valid answer. It must be delivered.
        async with LoopbackServer([0.9, *complete("Réponse tardive mais valide.")]) as server:
            turn, events, error = await self.collect(self.client(server))
        self.assertIsNone(error)
        self.assertEqual(turn.metrics["status"], "PASS")
        self.assertEqual(turn.delivered_text, "Réponse tardive mais valide.")
        self.assertGreater(turn.metrics["first_content_s"], SCALED.idle_timeout)

    async def test_b_keepalive_comments_are_silent_and_first_content_bounded(self):
        async with LoopbackServer([b": keep-alive\n\n", 0.3] * 12) as server:
            turn, events, error = await self.collect(self.client(server))
        self.assertEqual(error, "first_content_timeout")
        self.assertEqual(events, [])
        self.assertLess(
            turn.metrics["elapsed_s"], SCALED.first_content_timeout + SCALED.idle_timeout
        )

    async def test_c_idle_after_first_content_stays_bounded(self):
        async with LoopbackServer([event("Bonjour. "), 5.0]) as server:
            turn, events, error = await self.collect(self.client(server))
        self.assertEqual(error, "idle_timeout")
        self.assertEqual(turn.delivered_text, "Bonjour. ")
        self.assertLess(turn.metrics["elapsed_s"], SCALED.first_content_timeout)

    async def test_d_no_content_until_first_content_deadline(self):
        async with LoopbackServer([5.0]) as server:
            turn, events, error = await self.collect(self.client(server))
        self.assertEqual(error, "first_content_timeout")
        self.assertEqual(events, [])
        self.assertGreaterEqual(turn.metrics["elapsed_s"], SCALED.first_content_timeout)
        self.assertLess(
            turn.metrics["elapsed_s"], SCALED.first_content_timeout + SCALED.idle_timeout
        )

    async def test_e_cancel_during_first_content_wait_closes_cleanly(self):
        async with LoopbackServer([5.0]) as server:
            client = self.client(server)
            async with client.turn("Explique un réseau local.") as turn:
                await asyncio.sleep(0.2)  # Leaving the context cancels the pending read.
            self.assertEqual((turn.error, turn.metrics["status"]), ("cancelled", "CANCELLED"))
            self.assertTrue(turn.task.done() and turn.queue.empty())
            await self.until(lambda: server.closed_by_client == 1)
            self.assertLess(turn.metrics["elapsed_s"], SCALED.idle_timeout)

    async def test_f_client_reusable_after_first_content_timeout(self):
        async with LoopbackServer([5.0], complete("Deuxième tour.")) as server:
            client = self.client(server)
            turn, _, error = await self.collect(client)
            self.assertEqual(error, "first_content_timeout")
            await self.until(lambda: server.closed_by_client == 1)
            turn, events, error = await self.collect(client)
            self.assertIsNone(error)
            self.assertEqual(turn.delivered_text, "Deuxième tour.")
            self.assertEqual(server.connections, 2)

    async def test_g_single_comment_then_content_survives_segment_timers(self):
        # Synthetic comment: the incident retained its byte count, not its raw bytes.
        comment = b": offline-fixture\n\n"
        async with LoopbackServer([comment, 0.9, *complete("Contenu de test.")]) as server:
            turn, _, error = await self.collect(self.client(server))
        self.assertIsNone(error)
        self.assertEqual(turn.delivered_text, "Contenu de test.")
        self.assertEqual(turn.metrics["wire"]["comments"], 1)
        self.assertGreater(turn.metrics["first_content_s"], SCALED.idle_timeout)
        self.assertLess(turn.metrics["first_content_s"], SCALED.first_content_timeout)
        self.assertEqual(server.connections, 1)

        timeline = turn.metrics["timeline"]
        self.assertEqual(timeline["read_chunks"], turn.metrics["wire"]["chunks"])
        self.assertEqual(timeline["read_bytes"], turn.metrics["wire"]["bytes"])
        self.assertGreater(turn.metrics["wire"]["events"], 0)
        self.assertEqual(timeline["read_operations"], timeline["read_chunks"])
        self.assertFalse(timeline["read_pending_at_stop"])
        self.assertLess(timeline["first_read_received_s"], turn.metrics["first_content_s"])
        self.assertLessEqual(timeline["last_read_received_s"], timeline["stop_s"])
        self.assertLessEqual(turn.metrics["first_content_s"], timeline["stop_s"])
        self.assertEqual(turn.metrics["requests"], 1)

    async def test_h_single_comment_then_silence_has_no_content_and_closes(self):
        comment = b": offline-fixture\n\n"
        async with LoopbackServer([comment, 5.0]) as server:
            turn, events, error = await self.collect(self.client(server))
            await self.until(lambda: server.closed_by_client == 1)
        self.assertEqual(error, "first_content_timeout")
        self.assertEqual(events, [])
        self.assertEqual(turn.metrics["http_status"], 200)
        self.assertEqual(
            turn.metrics["wire"],
            {"chunks": 1, "bytes": len(comment), "events": 0, "comments": 1},
        )
        self.assertIsNone(turn.metrics["first_content_s"])
        self.assertEqual(turn.metrics["generated_chars"], 0)
        self.assertNotIn("transport_exception", turn.metrics)
        self.assertGreaterEqual(turn.metrics["elapsed_s"], SCALED.first_content_timeout)
        self.assertLess(
            turn.metrics["elapsed_s"], SCALED.first_content_timeout + SCALED.idle_timeout
        )
        self.assertEqual(server.connections, 1)
        self.assertTrue(turn.task.done() and turn.queue.empty())

        timeline = turn.metrics["timeline"]
        self.assertEqual(timeline["read_chunks"], 1)
        self.assertEqual(timeline["read_bytes"], len(comment))
        self.assertEqual(timeline["read_operations"], 2)  # Timer wakeups reuse the pending read.
        self.assertTrue(timeline["read_pending_at_stop"])
        milestones = [
            timeline[key]
            for key in (
                "request_prepared_s",
                "network_started_s",
                "headers_received_s",
                "first_read_started_s",
                "first_read_received_s",
                "last_read_started_s",
                "stop_s",
                "response_closed_s",
                "finalized_s",
            )
        ]
        self.assertEqual(milestones, sorted(milestones))
        self.assertGreaterEqual(timeline["stop_s"], SCALED.first_content_timeout)
        self.assertNotIn(comment.decode().strip(), json.dumps(turn.metrics))


if __name__ == "__main__":
    unittest.main()
