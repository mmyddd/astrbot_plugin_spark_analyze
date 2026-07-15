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
        def debug(*_args, **_kwargs):
            return None

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


def thread_hotspot_summary(index, name, total, scores):
    hotspots = tuple(
        main.Hotspot(
            path=f"{name}.hotspot{hotspot_index}",
            score=score,
            share=score / total * 100 if total else 0.0,
            source="",
            global_share=score,
            thread_name=name,
            thread_index=index,
        )
        for hotspot_index, score in enumerate(scores, start=1)
    )
    return main.ThreadHotspotSummary(
        name=name,
        total=total,
        hotspots=hotspots,
        thread_index=index,
    )


def profile_thread(name, total, scores):
    children = [
        {
            "className": f"com.example.{name.replace(' ', '')}{index}",
            "methodName": "hotspot",
            "lineNumber": index,
            "times": [score],
            "childrenRefs": [],
        }
        for index, score in enumerate(scores, start=1)
    ]
    return {
        "name": name,
        "times": [total],
        "childrenRefs": list(range(len(children))),
        "children": children,
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


class ErrorHttpClient:
    def stream(self, method, url, **kwargs):
        raise main.httpx.ConnectError("network unavailable")


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


class FakeResponsesResource:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if kwargs.get("stream"):
            return self.owner.stream
        return self.owner.response


class FakeResponseStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __aiter__(self):
        async def iterate():
            for event in self.events:
                yield event

        return iterate()

    async def close(self):
        self.closed = True


class FakeAsyncOpenAI:
    instances = []
    next_response = types.SimpleNamespace(output_text="Responses API 结果")
    next_stream_events = [
        types.SimpleNamespace(
            type="response.output_text.delta",
            delta="Responses API 结果",
        ),
        types.SimpleNamespace(
            type="response.completed",
            response=types.SimpleNamespace(output_text="Responses API 结果"),
        ),
    ]

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.response = self.__class__.next_response
        self.stream = FakeResponseStream(self.__class__.next_stream_events)
        self.responses = FakeResponsesResource(self)
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self):
        self.closed = True


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
            main.extract_spark_profile_link("xhttps://spark.lucko.me/abc123")
        )
        self.assertIsNone(
            main.extract_spark_profile_link("https://spark.lucko.me/abc123?full=true")
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

    def test_config_parses_and_bounds_values_once(self):
        config = main.SparkAnalyzeConfig.from_object(
            {
                "enabled_group_ids": [12345, "67890"],
                "llm_providers": [
                    {"name": "Primary", "__template_key": "astrbot_provider"},
                    "invalid",
                ],
                "llm_max_tokens": 999999,
                "llm_timeout_seconds": "0",
                "reasoning_effort": " high ",
                "debug_log_llm_response": "true",
                "max_profile_bytes": 1,
                "max_json_bytes": 999999999,
                "max_summary_chars": 1,
                "max_hotspots": 0,
                "max_threads": 999,
                "request_timeout_seconds": "12.5",
            }
        )

        self.assertEqual(config.enabled_group_ids, ("12345", "67890"))
        self.assertEqual(len(config.llm_providers), 1)
        self.assertEqual(config.llm_max_tokens, main.MAX_LLM_MAX_TOKENS)
        self.assertEqual(config.llm_timeout_seconds, 1)
        self.assertEqual(config.reasoning_effort, "high")
        self.assertTrue(config.debug_log_llm_response)
        self.assertEqual(config.max_profile_bytes, 1024)
        self.assertEqual(config.max_json_bytes, main.MAX_JSON_BYTES)
        self.assertEqual(config.max_summary_chars, 1000)
        self.assertEqual(config.max_hotspots, 1)
        self.assertEqual(config.max_threads, main.MAX_THREADS)
        self.assertEqual(config.request_timeout_seconds, 12.5)

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

    def test_fetch_spark_json_rejects_oversized_payload(self):
        response = FakeResponse(
            b"0123456789",
            main.SPARK_JSON_CONTENT_TYPE,
        )
        response.url = types.SimpleNamespace(host="spark-json-service.lucko.me")
        client = FakeHttpClient(response)

        with self.assertRaises(main.SparkDataTooLarge):
            asyncio.run(main.fetch_spark_json("abc123", client=client, max_bytes=5))

    def test_fetch_spark_profile_wraps_network_error(self):
        with self.assertRaises(main.SparkFetchError):
            asyncio.run(main.fetch_spark_profile("abc123", client=ErrorHttpClient()))

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
        self.assertIn("第三方 source 自耗热点聚合", summary)
        self.assertIn("testmod: 20.00%", summary)
        self.assertNotIn("testmod: 80.00%", summary)

    def test_summarize_profile_uses_platform_spark_version_fallback(self):
        profile = sample_profile()
        del profile["metadata"]["sources"]["spark"]

        summary = main.summarize_spark_profile(profile)

        self.assertIn("spark 版本：2", summary)

    def test_summarize_profile_counts_shared_nodes_once(self):
        profile = sample_profile()
        profile["metadata"]["sources"] = {
            "sharedmod": {
                "name": "Shared Mod",
                "version": "1.0.0",
                "builtIn": False,
            }
        }
        profile["classSources"] = {
            "mod.Root": "sharedmod",
            "mod.Left": "sharedmod",
            "mod.Right": "sharedmod",
            "mod.Leaf": "sharedmod",
        }
        profile["threads"] = [
            {
                "name": "Server thread",
                "times": [10],
                "childrenRefs": [0],
                "children": [
                    {
                        "className": "mod.Root",
                        "methodName": "root",
                        "times": [10],
                        "childrenRefs": [1, 2],
                    },
                    {
                        "className": "mod.Left",
                        "methodName": "left",
                        "times": [10],
                        "childrenRefs": [3],
                    },
                    {
                        "className": "mod.Right",
                        "methodName": "right",
                        "times": [10],
                        "childrenRefs": [3],
                    },
                    {
                        "className": "mod.Leaf",
                        "methodName": "leaf",
                        "times": [10],
                        "childrenRefs": [],
                    },
                ],
            }
        ]

        summary = main.summarize_spark_profile(
            profile,
            max_hotspots=10,
            max_chars=10000,
        )

        self.assertEqual(summary.count("mod.Leaf.leaf"), 1)
        self.assertIn("共享调用上下文=2", summary)
        self.assertIn("sharedmod: 100.00%", summary)

    def test_select_representative_hotspots_weights_slots_by_thread_samples(self):
        summaries = [
            thread_hotspot_summary(0, "High", 100, [50, 30, 20, 10]),
            thread_hotspot_summary(1, "Medium", 40, [20, 10, 5]),
            thread_hotspot_summary(2, "Low", 20, [10, 5]),
        ]

        selected_threads, selected_hotspots = (
            main._select_representative_hotspots(
                summaries,
                max_threads=3,
                max_hotspots=6,
            )
        )

        self.assertEqual(
            [thread.name for thread in selected_threads],
            ["High", "Medium", "Low"],
        )
        self.assertEqual(
            {
                name: sum(
                    hotspot.thread_name == name for hotspot in selected_hotspots
                )
                for name in ("High", "Medium", "Low")
            },
            {"High": 3, "Medium": 2, "Low": 1},
        )

    def test_select_representative_hotspots_maximizes_remaining_sample_coverage(self):
        summaries = [
            thread_hotspot_summary(0, "High diffuse", 100, [5, 5, 5, 5]),
            thread_hotspot_summary(1, "Medium concentrated", 40, [20, 10]),
            thread_hotspot_summary(2, "Low", 20, [10]),
        ]

        selected_threads, selected_hotspots = (
            main._select_representative_hotspots(
                summaries,
                max_threads=3,
                max_hotspots=5,
            )
        )

        self.assertEqual(
            [thread.name for thread in selected_threads],
            ["High diffuse", "Medium concentrated", "Low"],
        )
        self.assertEqual(sum(hotspot.score for hotspot in selected_hotspots), 50)
        self.assertEqual(
            {
                name: sum(
                    hotspot.thread_name == name for hotspot in selected_hotspots
                )
                for name in ("High diffuse", "Medium concentrated", "Low")
            },
            {"High diffuse": 2, "Medium concentrated": 2, "Low": 1},
        )

    def test_select_representative_hotspots_limits_threads_and_skips_empty_threads(
        self,
    ):
        summaries = [
            thread_hotspot_summary(0, "No hotspot", 100, []),
            thread_hotspot_summary(1, "High", 80, [50, 30]),
            thread_hotspot_summary(2, "Low", 10, [10]),
        ]

        selected_threads, selected_hotspots = (
            main._select_representative_hotspots(
                summaries,
                max_threads=8,
                max_hotspots=2,
            )
        )

        self.assertEqual([thread.name for thread in selected_threads], ["High"])
        self.assertEqual(len(selected_hotspots), 2)
        self.assertEqual(
            {hotspot.thread_name for hotspot in selected_hotspots},
            {"High"},
        )

    def test_select_representative_hotspots_jointly_optimizes_threads_and_slots(self):
        summaries = [
            thread_hotspot_summary(0, "Root-heavy weak", 100, [1]),
            thread_hotspot_summary(1, "Lower-root strong", 90, [90]),
        ]

        selected_threads, selected_hotspots = (
            main._select_representative_hotspots(
                summaries,
                max_threads=1,
                max_hotspots=1,
            )
        )

        self.assertEqual([thread.name for thread in selected_threads], ["Lower-root strong"])
        self.assertEqual([hotspot.score for hotspot in selected_hotspots], [90])

    def test_collect_hotspot_infers_source_from_call_path(self):
        thread = {
            "name": "Render thread",
            "times": [100],
            "childrenRefs": [0],
            "children": [
                {
                    "className": "com.example.ModEntry",
                    "methodName": "render",
                    "times": [100],
                    "childrenRefs": [1],
                },
                {
                    "className": "net.minecraft.Render",
                    "methodName": "draw",
                    "times": [100],
                    "childrenRefs": [2],
                },
                {
                    "className": "org.lwjgl.opengl.GL",
                    "methodName": "nativeDraw",
                    "times": [100],
                    "childrenRefs": [],
                },
            ],
        }

        hotspots, total = main._collect_thread_hotspots(
            thread,
            {
                "com.example.ModEntry": "testmod",
                "net.minecraft.Render": "minecraft",
            },
        )

        self.assertEqual(total, 100)
        self.assertEqual(len(hotspots), 1)
        self.assertEqual(hotspots[0].source, "testmod")
        self.assertTrue(hotspots[0].source_inferred)

        core_only_thread = {
            "name": "Render thread",
            "times": [100],
            "childrenRefs": [0],
            "children": [
                {
                    "className": "net.minecraft.Render",
                    "methodName": "draw",
                    "times": [100],
                    "childrenRefs": [1],
                },
                {
                    "className": "org.lwjgl.opengl.GL",
                    "methodName": "nativeDraw",
                    "times": [100],
                    "childrenRefs": [],
                },
            ],
        }
        core_hotspots, _ = main._collect_thread_hotspots(
            core_only_thread,
            {"net.minecraft.Render": "minecraft"},
        )
        self.assertEqual(core_hotspots[0].source, "")
        self.assertFalse(core_hotspots[0].source_inferred)

    def test_summary_reports_inferred_source_and_accounting_coverage(self):
        profile = sample_profile()
        profile["classSources"] = {
            "com.example.ModEntry": "testmod",
            "net.minecraft.Render": "minecraft",
        }
        profile["threads"] = [
            {
                "name": "Render thread",
                "times": [100],
                "childrenRefs": [0],
                "children": [
                    {
                        "className": "com.example.ModEntry",
                        "methodName": "render",
                        "times": [100],
                        "childrenRefs": [1],
                    },
                    {
                        "className": "net.minecraft.Render",
                        "methodName": "draw",
                        "times": [100],
                        "childrenRefs": [2],
                    },
                    {
                        "className": "org.lwjgl.opengl.GL",
                        "methodName": "nativeDraw",
                        "times": [100],
                        "childrenRefs": [],
                    },
                ],
            }
        ]

        summary = main.summarize_spark_profile(
            profile,
            max_hotspots=1,
            max_threads=1,
            max_chars=10000,
        )

        self.assertIn("source=testmod（调用链推断）", summary)
        self.assertIn("调用图可解释自耗覆盖线程根采样：100.00%", summary)
        self.assertIn("已展开热点覆盖可解释自耗：100.00%", summary)
        self.assertIn("其中调用链推断占该 source 自耗=100.00%", summary)

    def test_summarize_profile_reports_thread_trimming_and_coverage(self):
        profile = sample_profile()
        profile["classSources"] = {}
        profile["threads"] = [
            profile_thread("High", 100, [50, 30, 20]),
            profile_thread("Medium", 40, [30, 10]),
            profile_thread("Low", 10, [10]),
        ]

        summary = main.summarize_spark_profile(
            profile,
            max_hotspots=4,
            max_threads=2,
            max_chars=10000,
        )

        self.assertIn(
            "High: 全局线程采样占比=66.67%, 可解释自耗=100.00%, 热点配额=3",
            summary,
        )
        self.assertIn(
            "Medium: 全局线程采样占比=26.67%, 可解释自耗=100.00%, 热点配额=1",
            summary,
        )
        self.assertIn(
            "其余 1 个线程未展开：合计占全局采样 6.67%",
            summary,
        )
        self.assertIn(
            "已展开热点覆盖可解释自耗：86.67%；覆盖全线程根采样：86.67%",
            summary,
        )
        self.assertNotIn("[Low]", summary)

    def test_analysis_prompt_requires_evidence_and_limits_hallucination(self):
        prompt = main.build_analysis_prompt(
            code="abc123",
            source_url="https://spark.lucko.me/abc123",
            sender="Alice (67890)",
            summary="summary text",
        )

        self.assertIn("总体结论", prompt)
        self.assertIn("不要编造", prompt)
        self.assertIn("调用链推断", prompt)
        self.assertIn("共享调用上下文", prompt)
        self.assertIn("未覆盖部分不能被当作不存在性能开销", prompt)
        self.assertIn("summary text", prompt)
        self.assertIn("abc123", prompt)

    def test_handler_logs_stops_reacts_and_uses_current_provider(self):
        original_fetch_profile = main.fetch_spark_profile
        original_fetch_json = main.fetch_spark_json
        original_debug = main.logger.debug
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
        main.logger.debug = capture_info
        self.addCleanup(
            lambda: setattr(main, "fetch_spark_profile", original_fetch_profile)
        )
        self.addCleanup(lambda: setattr(main, "fetch_spark_json", original_fetch_json))
        self.addCleanup(lambda: setattr(main.logger, "debug", original_debug))

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

    def test_handler_ignores_mixed_message(self):
        context = FakeContext()
        plugin = main.SparkAnalyzePlugin(
            context,
            {"enabled_group_ids": ["12345"]},
        )
        event = FakeEvent(
            [
                Comp.Plain("https://spark.lucko.me/abc123"),
                types.SimpleNamespace(text="image"),
            ],
        )

        results = collect_async_generator(plugin.on_group_message(event))

        self.assertEqual(results, [])
        self.assertFalse(event.stopped)
        self.assertEqual(event.bot.emoji_like_calls, [])
        asyncio.run(plugin.terminate())

    def test_handler_continues_without_message_id(self):
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

        plugin = main.SparkAnalyzePlugin(
            FakeContext(),
            {"enabled_group_ids": ["12345"]},
        )
        event = FakeEvent(
            [Comp.Plain("https://spark.lucko.me/abc123")],
            message_id=None,
        )

        results = collect_async_generator(plugin.on_group_message(event))

        self.assertEqual(len(results), 1)
        self.assertTrue(event.stopped)
        self.assertEqual(event.bot.emoji_like_calls, [])
        asyncio.run(plugin.terminate())

    def test_handler_releases_code_after_analysis_failure(self):
        original_fetch_profile = main.fetch_spark_profile
        original_fetch_json = main.fetch_spark_json
        calls = []

        async def fake_fetch_profile(code, **kwargs):
            calls.append(("profile", code))
            return b"profile"

        async def failing_fetch_json(code, **kwargs):
            calls.append(("json", code))
            if len([item for item in calls if item[0] == "json"]) == 1:
                raise main.SparkFetchError("invalid profile")
            return sample_profile()

        main.fetch_spark_profile = fake_fetch_profile
        main.fetch_spark_json = failing_fetch_json
        self.addCleanup(
            lambda: setattr(main, "fetch_spark_profile", original_fetch_profile)
        )
        self.addCleanup(lambda: setattr(main, "fetch_spark_json", original_fetch_json))

        context = FakeContext()
        plugin = main.SparkAnalyzePlugin(
            context,
            {"enabled_group_ids": ["12345"]},
        )

        first_event = FakeEvent([Comp.Plain("https://spark.lucko.me/abc123")])
        first_results = collect_async_generator(plugin.on_group_message(first_event))
        second_event = FakeEvent([Comp.Plain("https://spark.lucko.me/abc123")])
        second_results = collect_async_generator(plugin.on_group_message(second_event))

        self.assertEqual(first_results, [])
        self.assertEqual(len(second_results), 1)
        self.assertNotIn("abc123", plugin._in_flight_codes)
        asyncio.run(plugin.terminate())

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

    def test_terminate_waits_for_active_task_and_clears_in_flight_codes(self):
        plugin = main.SparkAnalyzePlugin(FakeContext())
        plugin._in_flight_codes.add("abc123")

        async def exercise():
            started = asyncio.Event()
            release = asyncio.Event()

            async def active_worker():
                task = asyncio.current_task()
                plugin._active_tasks.add(task)
                started.set()
                await release.wait()
                plugin._active_tasks.discard(task)

            worker = asyncio.create_task(active_worker())
            await started.wait()
            terminating = asyncio.create_task(plugin.terminate())
            await asyncio.sleep(0)
            self.assertFalse(terminating.done())
            release.set()
            await worker
            await terminating

        asyncio.run(exercise())
        self.assertEqual(plugin._in_flight_codes, set())

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

    def test_debug_log_llm_response_applies_to_astrbot_provider(self):
        original_debug = main.logger.debug
        logs = []
        main.logger.debug = lambda *args, **kwargs: logs.append((args, kwargs))
        self.addCleanup(lambda: setattr(main.logger, "debug", original_debug))

        result = asyncio.run(
            main.generate_analysis(
                FakeContext(),
                FakeEvent([]),
                "prompt",
                {
                    "debug_log_llm_response": True,
                    "llm_providers": [
                        {
                            "__template_key": "astrbot_provider",
                            "name": "Current",
                        }
                    ],
                },
                types.SimpleNamespace(),
            )
        )

        self.assertIn("性能分析结果", result)
        self.assertTrue(
            any("性能分析结果" in str(args) for args, _ in logs)
        )

    def test_provider_falls_back_when_template_is_unknown(self):
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
                            "__template_key": "unsupported",
                            "name": "Unsupported",
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

    def test_responses_api_uses_sdk_payload_and_normalizes_base_url(self):
        original_client = main.AsyncOpenAI
        FakeAsyncOpenAI.instances.clear()
        main.AsyncOpenAI = FakeAsyncOpenAI
        self.addCleanup(lambda: setattr(main, "AsyncOpenAI", original_client))

        result = asyncio.run(
            main._call_responses_api(
                "profile prompt",
                {
                    "name": "OpenAI Responses",
                    "api_key": "test-key",
                    "base_url": "https://example.com/",
                    "model": "gpt-5",
                },
                {
                    "llm_max_tokens": 2048,
                    "llm_timeout_seconds": 30,
                    "reasoning_effort": "high",
                },
            )
        )

        self.assertEqual(result, "Responses API 结果")
        instance = FakeAsyncOpenAI.instances[0]
        self.assertEqual(instance.kwargs["api_key"], "test-key")
        self.assertEqual(instance.kwargs["base_url"], "https://example.com/v1")
        self.assertEqual(instance.kwargs["timeout"], 30.0)
        self.assertEqual(instance.kwargs["max_retries"], 0)
        self.assertTrue(instance.closed)
        self.assertTrue(instance.stream.closed)
        self.assertEqual(
            instance.calls,
            [
                {
                    "model": "gpt-5",
                    "input": "profile prompt",
                    "max_output_tokens": 2048,
                    "reasoning": {"effort": "high"},
                    "stream": True,
                }
            ],
        )

    def test_responses_api_extracts_structured_output_when_output_text_empty(self):
        original_client = main.AsyncOpenAI
        FakeAsyncOpenAI.instances.clear()
        FakeAsyncOpenAI.next_stream_events = [
            types.SimpleNamespace(
                type="response.completed",
                response=types.SimpleNamespace(
                    output_text="",
                    output=[
                        types.SimpleNamespace(
                            content=[
                                types.SimpleNamespace(text="structured response"),
                            ]
                        )
                    ],
                ),
            )
        ]
        main.AsyncOpenAI = FakeAsyncOpenAI
        self.addCleanup(lambda: setattr(main, "AsyncOpenAI", original_client))
        self.addCleanup(
            lambda: setattr(
                FakeAsyncOpenAI,
                "next_response",
                types.SimpleNamespace(output_text="Responses API 结果"),
            )
        )
        self.addCleanup(
            lambda: setattr(
                FakeAsyncOpenAI,
                "next_stream_events",
                [
                    types.SimpleNamespace(
                        type="response.output_text.delta",
                        delta="Responses API 结果",
                    ),
                    types.SimpleNamespace(
                        type="response.completed",
                        response=types.SimpleNamespace(
                            output_text="Responses API 结果"
                        ),
                    ),
                ],
            )
        )

        result = asyncio.run(
            main._call_responses_api(
                "prompt",
                {
                    "api_key": "test-key",
                    "base_url": "https://example.com/v1",
                },
                {},
            )
        )

        self.assertEqual(result, "structured response")

    def test_generate_analysis_selects_responses_api_provider(self):
        original_call = main._call_responses_api
        calls = []

        async def fake_call(prompt, provider, config):
            calls.append((prompt, provider, config))
            return "Responses provider result"

        main._call_responses_api = fake_call
        self.addCleanup(lambda: setattr(main, "_call_responses_api", original_call))

        result = asyncio.run(
            main.generate_analysis(
                FakeContext(),
                FakeEvent([]),
                "prompt",
                {
                    "llm_providers": [
                        {
                            "__template_key": "responses_api",
                            "name": "Responses",
                        }
                    ]
                },
                types.SimpleNamespace(),
            )
        )

        self.assertEqual(result, "Responses provider result")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "prompt")

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
