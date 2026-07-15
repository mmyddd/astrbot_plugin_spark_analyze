from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star


SPARK_LINK_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])https://spark\.lucko\.me/"
    r"([A-Za-z0-9]{4,64})(?![A-Za-z0-9/?#])"
)
SPARK_RAW_BASE_URL = "https://spark-usercontent.lucko.me"
SPARK_JSON_BASE_URL = "https://spark-json-service.lucko.me"
SPARK_PROFILE_CONTENT_TYPE = "application/x-spark-sampler"
SPARK_JSON_CONTENT_TYPE = "application/json"

DEFAULT_MAX_PROFILE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_JSON_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_SUMMARY_CHARS = 60000
DEFAULT_MAX_HOTSPOTS = 20
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_LLM_TIMEOUT_SECONDS = 120
DEFAULT_LLM_MAX_TOKENS = 4096
DEFAULT_TERMINATE_WAIT_SECONDS = 10
MAX_PROFILE_BYTES = 50 * 1024 * 1024
MAX_JSON_BYTES = 50 * 1024 * 1024
MAX_SUMMARY_CHARS = 200000
MAX_HOTSPOTS = 100
MAX_REQUEST_TIMEOUT_SECONDS = 300
MAX_LLM_TIMEOUT_SECONDS = 600
MAX_LLM_MAX_TOKENS = 32768
MAX_PROFILE_TREE_NODES = 250000

OPENAI_COMPATIBLE_TEMPLATES = frozenset({"openai_compatible", "modelscope"})

PARSED_MESSAGE_EMOJI_ID = 289
PARSED_MESSAGE_EMOJI_TYPE = "1"


class SparkFetchError(RuntimeError):
    """Raised when a Spark payload cannot be fetched or validated."""


class SparkDataTooLarge(SparkFetchError):
    """Raised when a Spark response exceeds the configured limit."""


@dataclass(frozen=True)
class SparkProfileLink:
    code: str
    url: str


@dataclass(frozen=True)
class Hotspot:
    path: str
    score: float
    share: float
    source: str


def extract_spark_profile_link(text: object) -> SparkProfileLink | None:
    matches = list(SPARK_LINK_PATTERN.finditer(str(text or "")))
    if len(matches) != 1:
        return None

    match = matches[0]
    return SparkProfileLink(code=match.group(1), url=match.group(0))


def _config_get(config: object, key: str, default: Any) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        return config.get(key, default)
    return getattr(config, key, default)


def _bounded_config_int(
    config: object,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_config_get(config, key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def _bounded_config_float(
    config: object,
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(_config_get(config, key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def is_group_allowed(group_id: object, whitelist: object) -> bool:
    if not whitelist:
        return False
    return str(group_id) in {str(item) for item in whitelist}


def _get_group_id(event: AstrMessageEvent) -> object:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        group_id = getter()
        if group_id:
            return group_id

    message_obj = getattr(event, "message_obj", None)
    return getattr(message_obj, "group_id", None)


def _get_message_segments(event: AstrMessageEvent) -> list[object]:
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        messages = getter()
        if messages is not None:
            return list(messages)

    message_obj = getattr(event, "message_obj", None)
    return list(getattr(message_obj, "message", []) or [])


def _get_message_id(event: AstrMessageEvent) -> object:
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None)
    if isinstance(raw_message, dict):
        return raw_message.get("message_id")
    return getattr(raw_message, "message_id", None)


def _sender_text(event: AstrMessageEvent) -> str:
    sender_name = "未知发送者"
    sender_id = "未知ID"
    name_getter = getattr(event, "get_sender_name", None)
    id_getter = getattr(event, "get_sender_id", None)
    if callable(name_getter):
        sender_name = name_getter() or sender_name
    if callable(id_getter):
        sender_id = id_getter() or sender_id
    return f"{sender_name} ({sender_id})"


async def _react_to_parsed_message(event: AstrMessageEvent) -> None:
    bot = getattr(event, "bot", None)
    setter = getattr(bot, "set_msg_emoji_like", None)
    if not callable(setter):
        return

    message_id = _get_message_id(event)
    if message_id is None:
        return

    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        logger.warning("[SparkAnalyze] 无法获取有效 message_id：%s", message_id)
        return

    try:
        await setter(
            message_id=message_id,
            emoji_id=PARSED_MESSAGE_EMOJI_ID,
            emoji_type=PARSED_MESSAGE_EMOJI_TYPE,
            set=True,
        )
    except Exception:
        logger.exception(
            "[SparkAnalyze] 给已识别 Spark 消息贴表情失败：message_id=%s",
            message_id,
        )


def _build_http_client() -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_connections=50,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,
    )
    return httpx.AsyncClient(
        follow_redirects=True,
        http2=True,
        limits=limits,
    )


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def _read_limited_response(
    response: httpx.Response,
    max_bytes: int,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    async for chunk in response.aiter_bytes():
        total_size += len(chunk)
        if total_size > max_bytes:
            raise SparkDataTooLarge(
                f"{label} 大小超过限制：{total_size} > {max_bytes}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_response_host(response: httpx.Response, expected_host: str) -> None:
    response_url = getattr(response, "url", None)
    actual_host = getattr(response_url, "host", None)
    if actual_host and actual_host.lower() != expected_host:
        raise SparkFetchError(
            f"Spark 请求发生非预期跳转：{actual_host} != {expected_host}"
        )


async def fetch_spark_profile(
    code: str,
    max_bytes: int = DEFAULT_MAX_PROFILE_BYTES,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    owns_client = client is None
    request_client = client or _build_http_client()
    url = f"{SPARK_RAW_BASE_URL}/{code}"

    try:
        async with request_client.stream(
            "GET",
            url,
            headers={"Accept": SPARK_PROFILE_CONTENT_TYPE},
            timeout=timeout_seconds,
        ) as response:
            _validate_response_host(response, "spark-usercontent.lucko.me")
            response.raise_for_status()
            if _content_type(response) != SPARK_PROFILE_CONTENT_TYPE:
                raise SparkFetchError(
                    "Spark profile Content-Type 不正确："
                    f"{response.headers.get('content-type', '')}"
                )
            return await _read_limited_response(
                response,
                max_bytes=max_bytes,
                label="Spark profile",
            )
    except httpx.HTTPError as error:
        raise SparkFetchError(f"下载 Spark profile 失败：{url}") from error
    finally:
        if owns_client:
            await request_client.aclose()


async def fetch_spark_json(
    code: str,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    request_client = client or _build_http_client()
    url = f"{SPARK_JSON_BASE_URL}/{code}"

    try:
        async with request_client.stream(
            "GET",
            url,
            params={"full": "true"},
            headers={"Accept": SPARK_JSON_CONTENT_TYPE},
            timeout=timeout_seconds,
        ) as response:
            _validate_response_host(response, "spark-json-service.lucko.me")
            response.raise_for_status()
            content_type = _content_type(response)
            if content_type != SPARK_JSON_CONTENT_TYPE:
                raise SparkFetchError(
                    "Spark JSON Content-Type 不正确："
                    f"{response.headers.get('content-type', '')}"
                )
            payload = await _read_limited_response(
                response,
                max_bytes=max_bytes,
                label="Spark JSON",
            )
    except httpx.HTTPError as error:
        raise SparkFetchError(f"获取 Spark JSON 失败：{url}") from error
    finally:
        if owns_client:
            await request_client.aclose()

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SparkFetchError("Spark JSON 响应不是有效 JSON") from error
    if not isinstance(data, dict):
        raise SparkFetchError("Spark JSON 响应不是对象")
    if data.get("type") != "sampler":
        raise SparkFetchError(f"不支持的 Spark 数据类型：{data.get('type')!r}")
    if not isinstance(data.get("metadata"), dict):
        raise SparkFetchError("Spark JSON 缺少 metadata")
    if not isinstance(data.get("threads"), list):
        raise SparkFetchError("Spark JSON 缺少完整 threads 数据")
    return data


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sum_times(value: object) -> float:
    if not isinstance(value, list):
        return _safe_float(value)
    return sum(_safe_float(item) for item in value)


def _times_vector(value: object) -> list[float]:
    if isinstance(value, list):
        return [_safe_float(item) for item in value]
    if value is None:
        return []
    return [_safe_float(value)]


def _exclusive_times(
    node: Mapping[str, Any],
    nodes: list[object],
) -> float:
    own_times = _times_vector(node.get("times"))
    if not own_times:
        return 0.0

    child_refs = node.get("childrenRefs")
    if not isinstance(child_refs, list):
        return sum(max(value, 0.0) for value in own_times)

    child_totals = [0.0] * len(own_times)
    seen_refs: set[int] = set()
    for child_ref in child_refs:
        if (
            not isinstance(child_ref, int)
            or child_ref < 0
            or child_ref >= len(nodes)
            or child_ref in seen_refs
        ):
            continue
        seen_refs.add(child_ref)
        raw_child = nodes[child_ref]
        if not isinstance(raw_child, dict):
            continue
        values = _times_vector(raw_child.get("times"))
        for index, value in enumerate(values[: len(own_times)]):
            child_totals[index] += value

    return sum(
        max(own_value - child_totals[index], 0.0)
        for index, own_value in enumerate(own_times)
    )


def _format_number(value: object, digits: int = 2) -> str:
    number = _safe_float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}"


def _format_bytes(value: object) -> str:
    number = _safe_float(value)
    if number < 1024:
        return f"{int(number)} B"
    if number < 1024 * 1024:
        return f"{number / 1024:.1f} KiB"
    if number < 1024 * 1024 * 1024:
        return f"{number / (1024 * 1024):.1f} MiB"
    return f"{number / (1024 * 1024 * 1024):.2f} GiB"


def _format_percent(value: float) -> str:
    return f"{value:.2f}%"


def _node_label(node: Mapping[str, Any]) -> str:
    class_name = str(node.get("className") or "unknown")
    method_name = str(node.get("methodName") or "<unknown>")
    line_number = node.get("lineNumber")
    label = f"{class_name}.{method_name}"
    if isinstance(line_number, (int, float)) and line_number > 0:
        label += f":{int(line_number)}"
    return label


def _node_source(
    node: Mapping[str, Any],
    class_sources: Mapping[str, Any],
) -> str:
    class_name = str(node.get("className") or "")
    return str(class_sources.get(class_name) or "")


def _collect_thread_hotspots(
    thread: Mapping[str, Any],
    class_sources: Mapping[str, Any],
) -> tuple[list[Hotspot], float]:
    nodes = thread.get("children")
    root_refs = thread.get("childrenRefs")
    if not isinstance(nodes, list) or not isinstance(root_refs, list):
        return [], _sum_times(thread.get("times"))
    if len(nodes) > MAX_PROFILE_TREE_NODES:
        raise SparkFetchError(
            "Spark 线程采样树节点数超过限制："
            f"{len(nodes)} > {MAX_PROFILE_TREE_NODES}"
        )

    # Spark stores the call tree as a shared-reference graph. Walk each node
    # once, keep one representative root-to-node path, and derive self time by
    # subtracting the unique direct children from the inclusive node samples.
    paths: dict[int, list[str]] = {}
    queue: list[int] = []
    for root_ref in root_refs:
        if (
            isinstance(root_ref, int)
            and 0 <= root_ref < len(nodes)
            and root_ref not in paths
            and isinstance(nodes[root_ref], dict)
        ):
            paths[root_ref] = [_node_label(nodes[root_ref])]
            queue.append(root_ref)

    cursor = 0
    while cursor < len(queue):
        ref = queue[cursor]
        cursor += 1
        raw_node = nodes[ref]
        if not isinstance(raw_node, dict):
            continue
        child_refs = raw_node.get("childrenRefs")
        if not isinstance(child_refs, list):
            continue
        for child_ref in child_refs:
            if (
                not isinstance(child_ref, int)
                or child_ref < 0
                or child_ref >= len(nodes)
                or child_ref in paths
                or not isinstance(nodes[child_ref], dict)
            ):
                continue
            paths[child_ref] = [
                *paths[ref][-7:],
                _node_label(nodes[child_ref]),
            ]
            queue.append(child_ref)

    hotspots: list[Hotspot] = []
    for ref, labels in paths.items():
        raw_node = nodes[ref]
        if not isinstance(raw_node, dict):
            continue
        score = _exclusive_times(raw_node, nodes)
        if score <= 0:
            continue
        hotspots.append(
            Hotspot(
                path=" -> ".join(labels[-8:]),
                score=score,
                share=0.0,
                source=_node_source(raw_node, class_sources),
            )
        )

    thread_total = _sum_times(thread.get("times"))
    if thread_total > 0:
        hotspots = [
            Hotspot(
                path=item.path,
                score=item.score,
                share=item.score / thread_total * 100,
                source=item.source,
            )
            for item in hotspots
        ]
    return hotspots, thread_total


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n\n[摘要已截断，以控制 LLM 上下文长度]"
    return text[: max(0, max_chars - len(suffix))] + suffix


def summarize_spark_profile(
    profile: Mapping[str, Any],
    max_hotspots: int = DEFAULT_MAX_HOTSPOTS,
    max_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> str:
    metadata = profile.get("metadata")
    if not isinstance(metadata, dict):
        raise SparkFetchError("Spark profile 缺少 metadata")

    platform = metadata.get("platform")
    if not isinstance(platform, dict):
        platform = {}
    platform_name = platform.get("name") or "未知"
    platform_version = platform.get("version") or "未知"
    minecraft_version = platform.get("minecraftVersion") or "未知"

    start_time = _safe_float(metadata.get("startTime"))
    end_time = _safe_float(metadata.get("endTime"))
    duration_seconds = max(0.0, (end_time - start_time) / 1000)

    platform_stats = metadata.get("platformStatistics")
    if not isinstance(platform_stats, dict):
        platform_stats = {}
    tps = platform_stats.get("tps")
    if not isinstance(tps, dict):
        tps = {}
    mspt = platform_stats.get("mspt")
    if not isinstance(mspt, dict):
        mspt = {}
    mspt_last_minute = mspt.get("last1m")
    if not isinstance(mspt_last_minute, dict):
        mspt_last_minute = {}

    memory = platform_stats.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    heap = memory.get("heap")
    if not isinstance(heap, dict):
        heap = {}

    system_stats = metadata.get("systemStatistics")
    if not isinstance(system_stats, dict):
        system_stats = {}
    cpu = system_stats.get("cpu")
    if not isinstance(cpu, dict):
        cpu = {}
    process_usage = cpu.get("processUsage")
    if not isinstance(process_usage, dict):
        process_usage = {}

    world = platform_stats.get("world")
    if not isinstance(world, dict):
        world = {}

    sources = metadata.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    spark_source = sources.get("spark")
    if isinstance(spark_source, dict):
        spark_version = spark_source.get("version") or metadata.get(
            "sparkVersion",
            platform.get("sparkVersion", "未知"),
        )
    else:
        spark_version = metadata.get(
            "sparkVersion",
            platform.get("sparkVersion", "未知"),
        )

    lines = [
        "Spark profile 结构化摘要",
        f"- 平台：{platform_name} {platform_version}",
        f"- Minecraft：{minecraft_version}",
        f"- spark 版本：{spark_version}",
        f"- 采样时长：{_format_number(duration_seconds, 1)} 秒",
        f"- 采样间隔：{_format_number(metadata.get('interval'), 1)}",
        f"- 采样 tick 数：{metadata.get('numberOfTicks', '未知')}",
        "",
        "运行指标：",
        f"- TPS：1m={_format_number(tps.get('last1m'))}, "
        f"5m={_format_number(tps.get('last5m'))}, "
        f"15m={_format_number(tps.get('last15m'))}",
        f"- MSPT(1m)：均值={_format_number(mspt_last_minute.get('mean'))}, "
        f"最大={_format_number(mspt_last_minute.get('max'))}, "
        f"P95={_format_number(mspt_last_minute.get('percentile95'))}",
        f"- 堆内存：已用={_format_bytes(heap.get('used'))}, "
        f"已提交={_format_bytes(heap.get('committed'))}",
        f"- CPU：线程数={cpu.get('threads', '未知')}, "
        f"进程占用(1m)={_format_percent(_safe_float(process_usage.get('last1m')) * 100)}",
        f"- 玩家数：{platform_stats.get('playerCount', '未知')}",
        f"- 实体总数：{world.get('totalEntities', '未知')}",
        "",
    ]

    source_lines: list[str] = []
    for key, raw_source in sorted(sources.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_source, dict):
            continue
        name = raw_source.get("name") or key
        version = raw_source.get("version") or "未知版本"
        if raw_source.get("builtIn") is False:
            source_lines.append(f"{name}@{version}")
    lines.append(f"Mod/source 清单（共 {len(source_lines)} 个非内置条目）：")
    lines.extend(f"- {item}" for item in source_lines[:200])
    if len(source_lines) > 200:
        lines.append(f"- 其余 {len(source_lines) - 200} 个条目未展开")
    lines.append("")

    class_sources = profile.get("classSources")
    if not isinstance(class_sources, dict):
        class_sources = {}

    all_hotspots: list[Hotspot] = []
    total_sample_score = 0.0
    threads = profile.get("threads")
    if not isinstance(threads, list):
        threads = []
    for raw_thread in threads:
        if not isinstance(raw_thread, dict):
            continue
        thread_hotspots, thread_total = _collect_thread_hotspots(
            raw_thread,
            class_sources,
        )
        total_sample_score += thread_total
        thread_name = str(raw_thread.get("name") or "unknown thread")
        all_hotspots.extend(
            Hotspot(
                path=f"[{thread_name}] {item.path}",
                score=item.score,
                share=item.share,
                source=item.source,
            )
            for item in thread_hotspots
        )

    all_hotspots.sort(key=lambda item: item.score, reverse=True)
    lines.append(
        "采样自耗热点（每个调用树节点只计一次自耗时，"
        f"全线程累计采样值={_format_number(total_sample_score)}）："
    )
    for index, hotspot in enumerate(all_hotspots[:max_hotspots], start=1):
        source_text = f"，source={hotspot.source}" if hotspot.source else ""
        lines.append(
            f"{index}. {_format_percent(hotspot.share)} "
            f"({_format_number(hotspot.score)}): {hotspot.path}{source_text}"
        )
    if not all_hotspots:
        lines.append("- 未找到可展开的线程采样树")
    lines.append("")

    third_party_scores: dict[str, float] = {}
    for hotspot in all_hotspots:
        source = hotspot.source.strip()
        if source and source.lower() not in {"java", "minecraft", "vanilla"}:
            third_party_scores[source] = (
                third_party_scores.get(source, 0.0) + hotspot.score
            )
    lines.append("第三方 source 自耗热点聚合：")
    if third_party_scores and total_sample_score > 0:
        for index, (source, score) in enumerate(
            sorted(third_party_scores.items(), key=lambda item: item[1], reverse=True)[
                :max_hotspots
            ],
            start=1,
        ):
            lines.append(
                f"{index}. {source}: {_format_percent(score / total_sample_score * 100)} "
                f"({_format_number(score)})"
            )
    else:
        lines.append("- 未找到可归属的第三方 source")

    return _truncate_text("\n".join(lines), max_chars)


def build_analysis_prompt(
    code: str,
    source_url: str,
    sender: str,
    summary: str,
) -> str:
    return f"""你是 Minecraft 性能分析专家。请根据下面的 Spark profile 结构化摘要，输出严谨、可执行的中文诊断。

来源链接：{source_url}
Spark code：{code}
发送者：{sender}

请严格按以下结构回答：
1. 总体结论：先用通俗语言说明当前最值得关注的性能问题；如果没有明显问题，也要明确说明。
2. 关键指标：解释 TPS、MSPT、内存、CPU 和采样时长代表什么，并指出异常或正常之处。
3. 主要热点：结合采样占比和完整调用路径，指出最可能的 CPU/渲染/主线程/IO/实体等瓶颈。
4. Mod/source 判断：只使用摘要中出现的 Mod、source、类名和版本；说明哪些只是相关线索，哪些证据较强。
5. 优先级建议：给出按优先级排序的排查或优化步骤，尽量具体到可以执行的操作。
6. 结论边界：说明采样数据无法证明什么，以及还需要哪些数据才能进一步确认。

要求：
- 不要把采样占比直接写成精确 CPU 百分比。
- 不要编造摘要中没有出现的 Mod、版本、异常或配置。
- 区分客户端渲染问题与服务端 tick 问题。
- 证据不足时明确说“不确定”，不要强行归因。

Spark profile 摘要：
{summary}
"""


def _provider_template(provider: Mapping[str, Any]) -> str:
    return str(
        provider.get("__template_key")
        or provider.get("template")
        or "openai_compatible"
    )


def _extract_provider_text(response_data: object, provider_name: str) -> str:
    if not isinstance(response_data, dict):
        raise ValueError(f"{provider_name} 返回内容不是 JSON 对象")
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"{provider_name} 返回内容缺少 choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError(f"{provider_name} 返回的 choice 无效")
    message = choice.get("message")
    content: object = None
    if isinstance(message, dict):
        content = message.get("content")
    if not content:
        content = choice.get("text")
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", item))
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    text = str(content or "").strip()
    if not text:
        raise ValueError(f"{provider_name} 返回内容为空")
    return text


async def _call_openai_compatible(
    client: httpx.AsyncClient,
    prompt: str,
    provider: Mapping[str, Any],
    config: object,
) -> str:
    provider_name = str(provider.get("name") or "OpenAI 兼容 Provider")
    api_key = str(provider.get("api_key") or "").strip()
    base_url = str(provider.get("base_url") or "").strip()
    model = str(provider.get("model") or "").strip() or "gpt-4o"
    if not api_key or not base_url:
        raise ValueError(f"{provider_name} 缺少 api_key 或 base_url")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _bounded_config_int(
            config,
            "llm_max_tokens",
            DEFAULT_LLM_MAX_TOKENS,
            256,
            MAX_LLM_MAX_TOKENS,
        ),
    }
    reasoning_effort = str(_config_get(config, "reasoning_effort", "") or "").strip()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    timeout_seconds = _bounded_config_float(
        config,
        "llm_timeout_seconds",
        DEFAULT_LLM_TIMEOUT_SECONDS,
        1,
        MAX_LLM_TIMEOUT_SECONDS,
    )
    response = await client.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    response_data = response.json()
    text = _extract_provider_text(response_data, provider_name)
    return text


async def _call_astrbot_provider(
    context: Context,
    event: AstrMessageEvent,
    prompt: str,
) -> str:
    provider_id = await context.get_current_chat_provider_id(
        umo=event.unified_msg_origin
    )
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=prompt,
    )
    text = str(getattr(response, "completion_text", response) or "").strip()
    if not text:
        raise ValueError("AstrBot Provider 返回内容为空")
    return text


async def generate_analysis(
    context: Context,
    event: AstrMessageEvent,
    prompt: str,
    config: object,
    client: httpx.AsyncClient,
) -> str:
    configured_providers = _config_get(config, "llm_providers", []) or []
    providers = list(configured_providers)
    if not providers:
        providers = [
            {
                "__template_key": "astrbot_provider",
                "name": "AstrBot Provider",
            }
        ]
        logger.info("[SparkAnalyze] 未配置 Provider 列表，使用当前 AstrBot Provider")

    last_error: Exception | None = None
    for provider in providers:
        if not isinstance(provider, Mapping):
            last_error = ValueError("Provider 配置不是对象")
            continue

        provider_name = str(provider.get("name") or "未命名 Provider")
        template = _provider_template(provider)
        try:
            logger.info(
                "[SparkAnalyze] 尝试 Provider：name=%s, template=%s",
                provider_name,
                template,
            )
            if template == "astrbot_provider":
                text = await _call_astrbot_provider(context, event, prompt)
            elif template in OPENAI_COMPATIBLE_TEMPLATES:
                text = await _call_openai_compatible(
                    client,
                    prompt,
                    provider,
                    config,
                )
            else:
                raise ValueError(f"不支持的 Provider 模板：{template or '未知'}")
            if _config_get(config, "debug_log_llm_response", False):
                logger.info("[SparkAnalyze] %s 返回：%s", provider_name, text)
            logger.info("[SparkAnalyze] Provider 分析成功：%s", provider_name)
            return text
        except Exception as error:
            last_error = error
            logger.warning(
                "[SparkAnalyze] Provider 调用失败：name=%s, error=%s",
                provider_name,
                error,
            )

    raise RuntimeError(f"所有 Spark 分析 Provider 均不可用：{len(providers)} 个") from last_error


def _build_forward_nodes(
    code: str,
    source_url: str,
    sender: str,
    analysis: str,
) -> Comp.Nodes:
    info = (
        f"Spark profile 来源\n"
        f"链接：{source_url}\n"
        f"code：{code}\n"
        f"发送者：{sender}"
    )
    return Comp.Nodes(
        [
            Comp.Node(
                uin="0",
                name="Spark profile 来源",
                content=[Comp.Plain(info)],
            ),
            Comp.Node(
                uin="0",
                name="LLM 性能分析",
                content=[Comp.Plain(analysis)],
            ),
        ]
    )


class SparkAnalyzePlugin(Star):
    def __init__(self, context: Context, config: object = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()
        self._in_flight_codes: set[str] = set()
        self._in_flight_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._terminating = False

    async def initialize(self) -> None:
        self._terminating = False

    async def _get_http_client(self) -> httpx.AsyncClient:
        async with self._http_client_lock:
            if self._terminating:
                raise RuntimeError("Spark 分析插件正在终止")
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = _build_http_client()
            return self._http_client

    async def _claim_code(self, code: str) -> bool:
        async with self._in_flight_lock:
            if code in self._in_flight_codes:
                return False
            self._in_flight_codes.add(code)
            return True

    async def _release_code(self, code: str) -> None:
        async with self._in_flight_lock:
            self._in_flight_codes.discard(code)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(
        self,
        event: AstrMessageEvent,
    ) -> AsyncIterator[object]:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_tasks.add(current_task)
        result: Comp.Nodes | None = None
        try:
            if self._terminating:
                return

            group_id = _get_group_id(event)
            whitelist = _config_get(self.config, "enabled_group_ids", [])
            if not is_group_allowed(group_id, whitelist):
                return

            messages = _get_message_segments(event)
            if len(messages) != 1 or not isinstance(messages[0], Comp.Plain):
                return

            link = extract_spark_profile_link(getattr(messages[0], "text", ""))
            if link is None:
                return

            logger.info(
                "[SparkAnalyze] 识别到 Spark profile 链接：url=%s, code=%s, group_id=%s",
                link.url,
                link.code,
                group_id,
            )
            event.stop_event()
            await _react_to_parsed_message(event)

            if not await self._claim_code(link.code):
                logger.info(
                    "[SparkAnalyze] code 已在分析中，跳过重复任务：code=%s",
                    link.code,
                )
                return

            try:
                max_profile_bytes = _bounded_config_int(
                    self.config,
                    "max_profile_bytes",
                    DEFAULT_MAX_PROFILE_BYTES,
                    1024,
                    MAX_PROFILE_BYTES,
                )
                max_json_bytes = _bounded_config_int(
                    self.config,
                    "max_json_bytes",
                    DEFAULT_MAX_JSON_BYTES,
                    1024,
                    MAX_JSON_BYTES,
                )
                max_summary_chars = _bounded_config_int(
                    self.config,
                    "max_summary_chars",
                    DEFAULT_MAX_SUMMARY_CHARS,
                    1000,
                    MAX_SUMMARY_CHARS,
                )
                max_hotspots = _bounded_config_int(
                    self.config,
                    "max_hotspots",
                    DEFAULT_MAX_HOTSPOTS,
                    1,
                    MAX_HOTSPOTS,
                )
                request_timeout_seconds = _bounded_config_float(
                    self.config,
                    "request_timeout_seconds",
                    DEFAULT_REQUEST_TIMEOUT_SECONDS,
                    1,
                    MAX_REQUEST_TIMEOUT_SECONDS,
                )

                client = await self._get_http_client()
                raw_profile = await fetch_spark_profile(
                    link.code,
                    max_bytes=max_profile_bytes,
                    timeout_seconds=request_timeout_seconds,
                    client=client,
                )
                if not raw_profile:
                    raise SparkFetchError("Spark profile 响应为空")
                logger.info(
                    "[SparkAnalyze] 已下载 Spark profile：code=%s, bytes=%s",
                    link.code,
                    len(raw_profile),
                )
                profile = await fetch_spark_json(
                    link.code,
                    max_bytes=max_json_bytes,
                    timeout_seconds=request_timeout_seconds,
                    client=client,
                )
                summary = summarize_spark_profile(
                    profile,
                    max_hotspots=max_hotspots,
                    max_chars=max_summary_chars,
                )
                sender = _sender_text(event)
                prompt = build_analysis_prompt(
                    code=link.code,
                    source_url=link.url,
                    sender=sender,
                    summary=summary,
                )
                analysis = await generate_analysis(
                    self.context,
                    event,
                    prompt,
                    self.config,
                    client,
                )
                result = _build_forward_nodes(
                    code=link.code,
                    source_url=link.url,
                    sender=sender,
                    analysis=analysis,
                )
            except Exception:
                logger.exception(
                    "[SparkAnalyze] 处理 Spark profile 失败：code=%s",
                    link.code,
                )
                return
            finally:
                await self._release_code(link.code)
        except Exception:
            logger.exception(
                "[SparkAnalyze] 处理群消息时发生异常",
            )
            return
        finally:
            if current_task is not None:
                self._active_tasks.discard(current_task)

        if result is not None:
            yield event.chain_result([result])

    async def terminate(self) -> None:
        self._terminating = True
        current_task = asyncio.current_task()
        active_tasks = [
            task
            for task in self._active_tasks
            if task is not current_task and not task.done()
        ]
        client: httpx.AsyncClient | None = None
        try:
            if active_tasks:
                done_tasks, pending_tasks = await asyncio.wait(
                    active_tasks,
                    timeout=DEFAULT_TERMINATE_WAIT_SECONDS,
                )
                if done_tasks:
                    await asyncio.gather(*done_tasks, return_exceptions=True)
                if pending_tasks:
                    logger.warning(
                        "[SparkAnalyze] 终止时仍有 %s 个分析任务未结束，开始取消",
                        len(pending_tasks),
                    )
                    for task in pending_tasks:
                        task.cancel()
                    cancelled_tasks, still_pending = await asyncio.wait(
                        pending_tasks,
                        timeout=1,
                    )
                    if cancelled_tasks:
                        await asyncio.gather(
                            *cancelled_tasks,
                            return_exceptions=True,
                        )
                    if still_pending:
                        logger.warning(
                            "[SparkAnalyze] 仍有 %s 个分析任务未响应取消",
                            len(still_pending),
                        )
        finally:
            async with self._http_client_lock:
                client = self._http_client
                self._http_client = None
            if client is not None and not client.is_closed:
                await client.aclose()

            async with self._in_flight_lock:
                self._in_flight_codes.clear()
