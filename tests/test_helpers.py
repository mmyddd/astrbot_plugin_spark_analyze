from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest


def _stub_astrbot_modules() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")

    class _EventMessageType:
        GROUP_MESSAGE = "group_message"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def event_message_type(message_type):
            def decorator(func):
                func._event_message_type = message_type
                return func

            return decorator

    class _AstrMessageEvent:
        pass

    class _Context:
        pass

    class _Star:
        def __init__(self, context):
            self.context = context

    class _Logger:
        @staticmethod
        def info(*_args, **_kwargs):
            return None

        @staticmethod
        def warning(*_args, **_kwargs):
            return None

        @staticmethod
        def exception(*_args, **_kwargs):
            return None

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _Node:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Nodes:
        def __init__(self, nodes):
            self.nodes = nodes

    components.Plain = _Plain
    components.Node = _Node
    components.Nodes = _Nodes
    event.filter = _Filter
    event.AstrMessageEvent = _AstrMessageEvent
    star.Context = _Context
    star.Star = _Star
    api.logger = _Logger
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.message_components"] = components


_stub_astrbot_modules()

import main
from astrbot.api import message_components as Comp


class FakeContext:
    def __init__(self):
        self.provider_calls = []
        self.generate_calls = []

    async def get_current_chat_provider_id(self, umo=None):
        self.provider_calls.append(umo)
        return "provider-default"

    async def llm_generate(self, chat_provider_id=None, prompt=None):
        self.generate_calls.append(
            {
                "chat_provider_id": chat_provider_id,
                "prompt": prompt,
            }
        )
        return types.SimpleNamespace(completion_text="性能分析结果：发现测试热点")


class FakeBot:
    def __init__(self, error=None, order=None):
        self.emoji_like_calls = []
        self.error = error
        self.order = order if order is not None else []

    async def set_msg_emoji_like(self, **kwargs):
        self.order.append("reaction")
        self.emoji_like_calls.append(kwargs)
        if self.error:
            raise self.error


class FakeEvent:
    def __init__(
        self,
        messages,
        group_id="12345",
        sender_name="Alice",
        sender_id="67890",
        message_id=987654,
        bot=None,
        order=None,
    ):
        self._messages = messages
        self._group_id = group_id
        self._sender_name = sender_name
        self._sender_id = sender_id
        self.order = order if order is not None else []
        self.bot = bot if bot is not None else FakeBot(order=self.order)
        self.unified_msg_origin = "umo://group/12345"
        self.stopped = False
        self.results = []
        self.message_obj = types.SimpleNamespace(
            message=messages,
            group_id=group_id,
            raw_message={"message_id": message_id},
        )

    def get_messages(self):
        return self._messages

    def get_group_id(self):
        return self._group_id

    def get_sender_name(self):
        return self._sender_name

    def get_sender_id(self):
        return self._sender_id

    def stop_event(self):
        self.order.append("stop")
        self.stopped = True

    def chain_result(self, chain):
        self.results.append(chain)
        return chain


def collect_async_generator(generator):
    async def collect():
        return [item async for item in generator]

    return asyncio.run(collect())


def sample_profile():
    return {
        "type": "sampler",
        "metadata": {
            "startTime": 1000,
            "endTime": 11000,
            "interval": 4000,
            "numberOfTicks": 200,
            "sparkVersion": 2,
            "platform": {
                "name": "Forge",
                "version": "47.4.1",
                "minecraftVersion": "1.20.1",
            },
            "platformStatistics": {
                "tps": {
                    "last1m": 19.5,
                    "last5m": 19.8,
                    "last15m": 20.0,
                },
                "mspt": {
                    "last1m": {
                        "mean": 12.5,
                        "max": 45.0,
                        "percentile95": 30.0,
                    }
                },
                "memory": {
                    "heap": {
                        "used": 2 * 1024 * 1024 * 1024,
                        "committed": 4 * 1024 * 1024 * 1024,
                    }
                },
                "playerCount": 3,
                "world": {"totalEntities": 42},
            },
            "systemStatistics": {
                "cpu": {
                    "threads": 8,
                    "processUsage": {"last1m": 0.25},
                }
            },
            "sources": {
                "testmod": {
                    "name": "Test Mod",
                    "version": "1.2.3",
                    "builtIn": False,
                },
                "minecraft": {
                    "name": "Minecraft",
                    "version": "1.20.1",
                    "builtIn": True,
                },
                "spark": {
                    "name": "spark",
                    "version": "1.10.53",
                    "builtIn": False,
                },
            },
        },
        "classSources": {
            "com.example.TestMod": "testmod",
            "net.minecraft.Server": "minecraft",
        },
        "threads": [
            {
                "name": "Server thread",
                "times": [100, 100],
                "children": [
                    {
                        "className": "com.example.TestMod",
                        "methodName": "tick",
                        "lineNumber": 10,
                        "times": [80, 80],
                        "childrenRefs": [1],
                    },
                    {
                        "className": "net.minecraft.Server",
                        "methodName": "run",
                        "lineNumber": 20,
                        "times": [60, 60],
                        "childrenRefs": [],
                    },
                ],
                "childrenRefs": [0],
            }
        ],
    }


class FakeResponse:
    def __init__(self, body, content_type):
        self.body = body
        self.headers = {"content-type": content_type}
        self.url = types.SimpleNamespace(host="spark-usercontent.lucko.me")

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self.body


class FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeStream(self.response)


class FakePostResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": "OpenAI 兼容 Provider 结果",
                    }
                }
            ]
        }


class FakePostClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakePostResponse()


class HelperTests(unittest.TestCase):
    def test_extract_spark_link_accepts_sample_and_trailing_punctuation(self):
        link = main.extract_spark_profile_link(
            "请分析：https://spark.lucko.me/02pGFymGbD。"
        )

        self.assertIsNotNone(link)
        self.assertEqual(link.code, "02pGFymGbD")
        self.assertEqual(link.url, "https://spark.lucko.me/02pGFymGbD")

    def test_extract_spark_link_rejects_other_domains_paths_and_multiple_links(self):
        self.assertIsNone(
            main.extract_spark_profile_link("https://example.com/02pGFymGbD")
        )
        self.assertIsNone(
            main.extract_spark_profile_link("https://spark.lucko.me/abc/extra")
        )
        self.assertIsNone(
            main.extract_spark_profile_link(
                "https://spark.lucko.me/abc123 https://spark.lucko.me/def456"
            )
        )

    def test_group_whitelist_normalizes_ids_and_denies_empty_list(self):
        self.assertTrue(main.is_group_allowed(12345, ["12345"]))
        self.assertFalse(main.is_group_allowed("12345", []))
        self.assertFalse(main.is_group_allowed("99999", ["12345"]))

    def test_fetch_spark_profile_validates_content_type_and_reads_bytes(self):
        client = FakeHttpClient(
            FakeResponse(b"profile", main.SPARK_PROFILE_CONTENT_TYPE)
        )

        result = asyncio.run(
            main.fetch_spark_profile("abc123", client=client, max_bytes=100)
        )

        self.assertEqual(result, b"profile")
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.calls[0][1], "https://spark-usercontent.lucko.me/abc123")
        self.assertEqual(
            client.calls[0][2]["headers"]["Accept"],
            main.SPARK_PROFILE_CONTENT_TYPE,
        )

    def test_fetch_spark_json_uses_full_query_and_parses_object(self):
        profile = sample_profile()
        response = FakeResponse(
            json.dumps({"type": "sampler", **profile}).encode("utf-8"),
            main.SPARK_JSON_CONTENT_TYPE,
        )
        response.url = types.SimpleNamespace(host="spark-json-service.lucko.me")
        client = FakeHttpClient(response)

        result = asyncio.run(main.fetch_spark_json("abc123", client=client))

        self.assertEqual(result["type"], "sampler")
        self.assertEqual(result["threads"][0]["name"], "Server thread")
        self.assertEqual(client.calls[0][2]["params"], {"full": "true"})

    def test_fetch_spark_profile_rejects_oversized_payload(self):
        client = FakeHttpClient(
            FakeResponse(b"0123456789", main.SPARK_PROFILE_CONTENT_TYPE)
        )

        with self.assertRaises(main.SparkDataTooLarge):
            asyncio.run(
                main.fetch_spark_profile("abc123", client=client, max_bytes=5)
            )

    def test_summarize_profile_rebuilds_refs_and_reports_source(self):
        summary = main.summarize_spark_profile(
            sample_profile(),
            max_hotspots=5,
            max_chars=10000,
        )

        self.assertIn("Forge 47.4.1", summary)
        self.assertIn("Minecraft：1.20.1", summary)
        self.assertIn("spark 版本：1.10.53", summary)
        self.assertIn("Test Mod@1.2.3", summary)
        self.assertIn("com.example.TestMod.tick:10", summary)
        self.assertIn("source=testmod", summary)
        self.assertIn("第三方 source 热点聚合", summary)

    def test_analysis_prompt_requires_evidence_and_limits_hallucination(self):
        prompt = main.build_analysis_prompt(
            code="abc123",
            source_url="https://spark.lucko.me/abc123",
            sender="Alice (67890)",
            summary="summary text",
        )

        self.assertIn("总体结论", prompt)
        self.assertIn("不要编造", prompt)
        self.assertIn("summary text", prompt)
        self.assertIn("abc123", prompt)

    def test_handler_logs_stops_reacts_and_uses_current_provider(self):
        original_fetch_profile = main.fetch_spark_profile
        original_fetch_json = main.fetch_spark_json
        original_info = main.logger.info
        calls = []
        order = []
        logs = []

        async def fake_fetch_profile(code, **kwargs):
            calls.append(("profile", code))
            order.append("profile")
            return b"profile"

        async def fake_fetch_json(code, **kwargs):
            calls.append(("json", code))
            order.append("json")
            return sample_profile()

        def capture_info(*args, **kwargs):
            logs.append((args, kwargs))

        main.fetch_spark_profile = fake_fetch_profile
        main.fetch_spark_json = fake_fetch_json
        main.logger.info = capture_info
        self.addCleanup(
            lambda: setattr(main, "fetch_spark_profile", original_fetch_profile)
        )
        self.addCleanup(lambda: setattr(main, "fetch_spark_json", original_fetch_json))
        self.addCleanup(lambda: setattr(main.logger, "info", original_info))

        context = FakeContext()
        plugin = main.SparkAnalyzePlugin(
            context,
            {"enabled_group_ids": ["12345"]},
        )
        bot = FakeBot(order=order)
        event = FakeEvent(
            [Comp.Plain("分析 https://spark.lucko.me/abc123")],
            bot=bot,
            order=order,
        )

        results = collect_async_generator(plugin.on_group_message(event))
        asyncio.run(plugin.terminate())

        self.assertTrue(event.stopped)
        self.assertEqual(order[:2], ["stop", "reaction"])
        self.assertEqual(
            bot.emoji_like_calls,
            [
                {
                    "message_id": 987654,
                    "emoji_id": 289,
                    "emoji_type": "1",
                    "set": True,
                }
            ],
        )
        self.assertEqual(calls, [("profile", "abc123"), ("json", "abc123")])
        self.assertEqual(context.provider_calls, ["umo://group/12345"])
        self.assertEqual(len(results), 1)
        self.assertIn("性能分析结果", results[0][0].nodes[1].kwargs["content"][0].text)
        self.assertTrue(any("识别到 Spark profile 链接" in args[0] for args, _ in logs))

    def test_handler_continues_when_reaction_fails(self):
        original_fetch_profile = main.fetch_spark_profile
        original_fetch_json = main.fetch_spark_json

        async def fake_fetch_profile(code, **kwargs):
            return b"profile"

        async def fake_fetch_json(code, **kwargs):
            return sample_profile()

        main.fetch_spark_profile = fake_fetch_profile
        main.fetch_spark_json = fake_fetch_json
        self.addCleanup(
            lambda: setattr(main, "fetch_spark_profile", original_fetch_profile)
        )
        self.addCleanup(lambda: setattr(main, "fetch_spark_json", original_fetch_json))

        context = FakeContext()
        plugin = main.SparkAnalyzePlugin(
            context,
            {"enabled_group_ids": ["12345"]},
        )
        event = FakeEvent(
            [Comp.Plain("https://spark.lucko.me/abc123")],
            bot=FakeBot(error=RuntimeError("reaction unavailable")),
        )

        results = collect_async_generator(plugin.on_group_message(event))
        asyncio.run(plugin.terminate())

        self.assertEqual(len(results), 1)
        self.assertEqual(len(context.generate_calls), 1)

    def test_handler_denies_unlisted_group_without_network_or_reaction(self):
        context = FakeContext()
        plugin = main.SparkAnalyzePlugin(context, {"enabled_group_ids": []})
        event = FakeEvent([Comp.Plain("https://spark.lucko.me/abc123")])

        results = collect_async_generator(plugin.on_group_message(event))

        self.assertEqual(results, [])
        self.assertFalse(event.stopped)
        self.assertEqual(event.bot.emoji_like_calls, [])
        self.assertEqual(context.generate_calls, [])

    def test_handler_skips_code_already_in_flight_after_acknowledging(self):
        context = FakeContext()
        plugin = main.SparkAnalyzePlugin(
            context,
            {"enabled_group_ids": ["12345"]},
        )
        asyncio.run(plugin._claim_code("abc123"))
        event = FakeEvent([Comp.Plain("https://spark.lucko.me/abc123")])

        results = collect_async_generator(plugin.on_group_message(event))

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.bot.emoji_like_calls), 1)
        self.assertEqual(context.generate_calls, [])
        asyncio.run(plugin.terminate())

    def test_provider_falls_back_to_astrbot_when_openai_config_is_invalid(self):
        context = FakeContext()
        client = types.SimpleNamespace()

        result = asyncio.run(
            main.generate_analysis(
                context,
                FakeEvent([]),
                "prompt",
                {
                    "llm_providers": [
                        {
                            "__template_key": "openai_compatible",
                            "name": "Invalid",
                            "api_key": "",
                            "base_url": "",
                        },
                        {
                            "__template_key": "astrbot_provider",
                            "name": "Fallback",
                        },
                    ]
                },
                client,
            )
        )

        self.assertIn("性能分析结果", result)
        self.assertEqual(context.provider_calls, ["umo://group/12345"])

    def test_openai_provider_sends_configured_payload(self):
        client = FakePostClient()

        result = asyncio.run(
            main._call_openai_compatible(
                client,
                "profile prompt",
                {
                    "name": "DeepSeek",
                    "api_key": "test-key",
                    "base_url": "https://example.com/",
                    "model": "deepseek-chat",
                },
                {
                    "llm_max_tokens": 2048,
                    "llm_timeout_seconds": 30,
                    "reasoning_effort": "high",
                },
            )
        )

        self.assertEqual(result, "OpenAI 兼容 Provider 结果")
        self.assertEqual(
            client.calls[0][0],
            "https://example.com/v1/chat/completions",
        )
        payload = client.calls[0][1]["json"]
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["messages"][0]["content"], "profile prompt")


if __name__ == "__main__":
    unittest.main()
