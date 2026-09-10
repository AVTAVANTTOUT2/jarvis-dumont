import asyncio
import contextlib
import dataclasses
import io
import json
import ssl
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
