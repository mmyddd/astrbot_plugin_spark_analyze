from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI

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
DEFAULT_MAX_THREADS = 8
DEFAULT_MAX_CONCURRENT_ANALYSES = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_LLM_TIMEOUT_SECONDS = 120
DEFAULT_LLM_MAX_TOKENS = 4096
DEFAULT_TERMINATE_WAIT_SECONDS = 10
MAX_PROFILE_BYTES = 50 * 1024 * 1024
MAX_JSON_BYTES = 50 * 1024 * 1024
MAX_SUMMARY_CHARS = 200000
MAX_HOTSPOTS = 100
MAX_THREADS = 64
MAX_CONCURRENT_ANALYSES = 8
MAX_REQUEST_TIMEOUT_SECONDS = 300
MAX_LLM_TIMEOUT_SECONDS = 600
MAX_LLM_MAX_TOKENS = 32768
MAX_PROFILE_TREE_NODES = 250000
MAX_PROFILE_THREADS = 512

OPENAI_COMPATIBLE_TEMPLATES = frozenset({"openai_compatible", "modelscope"})
RESPONSES_API_TEMPLATE = "responses_api"
CORE_SOURCE_IDS = frozenset({"java", "minecraft", "vanilla", "spark"})

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
    global_share: float = 0.0
    thread_name: str = ""
    thread_index: int = -1
    source_inferred: bool = False
    context_count: int = 1
    source_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThreadHotspotSummary:
    name: str
    total: float
    hotspots: tuple[Hotspot, ...]
    thread_index: int = -1


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


def _bounded_int(
    value: object,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _bounded_float(
    value: object,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _coerce_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _coerce_string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return (str(value),)
    return tuple(str(item) for item in value if str(item).strip())


def _coerce_provider_tuple(value: object) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


@dataclass(frozen=True)
class SparkAnalyzeConfig:
    enabled_group_ids: tuple[str, ...] = ()
    llm_providers: tuple[Mapping[str, Any], ...] = ()
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    reasoning_effort: str = ""
    debug_log_llm_response: bool = False
    max_profile_bytes: int = DEFAULT_MAX_PROFILE_BYTES
    max_json_bytes: int = DEFAULT_MAX_JSON_BYTES
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS
    max_hotspots: int = DEFAULT_MAX_HOTSPOTS
    max_threads: int = DEFAULT_MAX_THREADS
    max_concurrent_analyses: int = DEFAULT_MAX_CONCURRENT_ANALYSES
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    @classmethod
    def from_object(cls, raw: object) -> "SparkAnalyzeConfig":
        if isinstance(raw, cls):
            return raw
        return cls(
            enabled_group_ids=_coerce_string_tuple(
                _config_get(raw, "enabled_group_ids", [])
            ),
            llm_providers=_coerce_provider_tuple(
                _config_get(raw, "llm_providers", [])
            ),
            llm_max_tokens=_bounded_int(
                _config_get(raw, "llm_max_tokens", DEFAULT_LLM_MAX_TOKENS),
                DEFAULT_LLM_MAX_TOKENS,
                256,
                MAX_LLM_MAX_TOKENS,
            ),
            llm_timeout_seconds=_bounded_float(
                _config_get(
                    raw,
                    "llm_timeout_seconds",
                    DEFAULT_LLM_TIMEOUT_SECONDS,
                ),
                DEFAULT_LLM_TIMEOUT_SECONDS,
                1,
                MAX_LLM_TIMEOUT_SECONDS,
            ),
            reasoning_effort=str(
                _config_get(raw, "reasoning_effort", "") or ""
            ).strip(),
            debug_log_llm_response=_coerce_bool(
                _config_get(raw, "debug_log_llm_response", False)
            ),
            max_profile_bytes=_bounded_int(
                _config_get(raw, "max_profile_bytes", DEFAULT_MAX_PROFILE_BYTES),
                DEFAULT_MAX_PROFILE_BYTES,
                1024,
                MAX_PROFILE_BYTES,
            ),
            max_json_bytes=_bounded_int(
                _config_get(raw, "max_json_bytes", DEFAULT_MAX_JSON_BYTES),
                DEFAULT_MAX_JSON_BYTES,
                1024,
                MAX_JSON_BYTES,
            ),
            max_summary_chars=_bounded_int(
                _config_get(raw, "max_summary_chars", DEFAULT_MAX_SUMMARY_CHARS),
                DEFAULT_MAX_SUMMARY_CHARS,
                1000,
                MAX_SUMMARY_CHARS,
            ),
            max_hotspots=_bounded_int(
                _config_get(raw, "max_hotspots", DEFAULT_MAX_HOTSPOTS),
                DEFAULT_MAX_HOTSPOTS,
                1,
                MAX_HOTSPOTS,
            ),
            max_threads=_bounded_int(
                _config_get(raw, "max_threads", DEFAULT_MAX_THREADS),
                DEFAULT_MAX_THREADS,
                1,
                MAX_THREADS,
            ),
            max_concurrent_analyses=_bounded_int(
                _config_get(
                    raw,
                    "max_concurrent_analyses",
                    DEFAULT_MAX_CONCURRENT_ANALYSES,
                ),
                DEFAULT_MAX_CONCURRENT_ANALYSES,
                1,
                MAX_CONCURRENT_ANALYSES,
            ),
            request_timeout_seconds=_bounded_float(
                _config_get(
                    raw,
                    "request_timeout_seconds",
                    DEFAULT_REQUEST_TIMEOUT_SECONDS,
                ),
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
                1,
                MAX_REQUEST_TIMEOUT_SECONDS,
            ),
        )


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
    if len(data["threads"]) > MAX_PROFILE_THREADS:
        raise SparkFetchError(
            "Spark JSON 线程数超过限制："
            f"{len(data['threads'])} > {MAX_PROFILE_THREADS}"
        )
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


def _is_core_source(source: str) -> bool:
    return source.strip().lower() in CORE_SOURCE_IDS


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
    path_non_core_sources: dict[int, set[str]] = {}
    path_context_counts: dict[int, int] = {}
    seen_edges: set[tuple[int, int]] = set()
    queue: list[int] = []
    for root_ref in root_refs:
        if (
            isinstance(root_ref, int)
            and 0 <= root_ref < len(nodes)
            and root_ref not in paths
            and isinstance(nodes[root_ref], dict)
        ):
            paths[root_ref] = [_node_label(nodes[root_ref])]
            root_source = _node_source(nodes[root_ref], class_sources)
            path_non_core_sources[root_ref] = (
                {root_source}
                if root_source and not _is_core_source(root_source)
                else set()
            )
            path_context_counts[root_ref] = 1
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
                or not isinstance(nodes[child_ref], dict)
            ):
                continue
            edge_key = (ref, child_ref)
            is_new_edge = edge_key not in seen_edges
            seen_edges.add(edge_key)
            child_source = _node_source(nodes[child_ref], class_sources)
            if child_source and not _is_core_source(child_source):
                incoming_sources = {child_source}
            else:
                incoming_sources = set(path_non_core_sources.get(ref, set()))
            if child_ref in paths:
                if is_new_edge:
                    path_context_counts[child_ref] = (
                        path_context_counts.get(child_ref, 1) + 1
                    )
                current_sources = path_non_core_sources.setdefault(
                    child_ref,
                    set(),
                )
                previous_count = len(current_sources)
                current_sources.update(incoming_sources)
                if len(current_sources) > previous_count:
                    queue.append(child_ref)
                continue
            paths[child_ref] = [
                *paths[ref][-7:],
                _node_label(nodes[child_ref]),
            ]
            path_non_core_sources[child_ref] = incoming_sources
            path_context_counts[child_ref] = 1
            queue.append(child_ref)

    hotspots: list[Hotspot] = []
    for ref, labels in paths.items():
        raw_node = nodes[ref]
        if not isinstance(raw_node, dict):
            continue
        score = _exclusive_times(raw_node, nodes)
        if score <= 0:
            continue
        direct_source = _node_source(raw_node, class_sources)
        path_sources = tuple(sorted(path_non_core_sources.get(ref, set())))
        if direct_source:
            source = direct_source
            source_inferred = False
            source_candidates = ()
        elif len(path_sources) == 1:
            source = path_sources[0]
            source_inferred = True
            source_candidates = ()
        elif path_sources:
            source = ""
            source_inferred = True
            source_candidates = path_sources
        else:
            source = ""
            source_inferred = False
            source_candidates = ()
        hotspots.append(
            Hotspot(
                path=" -> ".join(labels[-8:]),
                score=score,
                share=0.0,
                source=source,
                source_inferred=source_inferred,
                context_count=path_context_counts.get(ref, 1),
                source_candidates=source_candidates,
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
                source_inferred=item.source_inferred,
                context_count=item.context_count,
                source_candidates=item.source_candidates,
            )
            for item in hotspots
        ]
    return hotspots, thread_total


def _select_representative_hotspots(
    thread_summaries: list[ThreadHotspotSummary],
    max_threads: int,
    max_hotspots: int,
) -> tuple[list[ThreadHotspotSummary], list[Hotspot]]:
    if max_threads <= 0 or max_hotspots <= 0:
        return [], []

    ranked_threads = sorted(
        (
            ThreadHotspotSummary(
                name=thread.name,
                total=thread.total,
                hotspots=tuple(
                    sorted(
                        (
                            hotspot
                            for hotspot in thread.hotspots
                            if hotspot.score > 0
                        ),
                        key=lambda hotspot: (
                            -hotspot.score,
                            -hotspot.global_share,
                            hotspot.path,
                        ),
                    )[:max_hotspots]
                ),
                thread_index=thread.thread_index,
            )
            for thread in thread_summaries
            if thread.total > 0 and thread.hotspots
        ),
        key=lambda thread: (
            -thread.total,
            thread.thread_index if thread.thread_index >= 0 else 0,
            thread.name,
        ),
    )
    if not ranked_threads:
        return [], []

    thread_limit = min(max_threads, max_hotspots, len(ranked_threads))
    hotspot_limit = min(
        max_hotspots,
        sum(len(thread.hotspots) for thread in ranked_threads),
    )
    if thread_limit <= 0 or hotspot_limit <= 0:
        return [], []

    # Multiple-choice knapsack: allocate at least one hotspot to every chosen
    # thread and maximize the total selected self-sample score. This jointly
    # chooses threads and hotspots, so a high-root-sample thread with weak
    # evidence cannot displace a lower-root-sample thread with a strong hotspot.
    states: dict[tuple[int, int], float] = {(0, 0): 0.0}
    parent_layers: list[dict[tuple[int, int], int]] = []

    for thread in ranked_threads:
        prefix_scores = [0.0]
        for hotspot in thread.hotspots:
            prefix_scores.append(prefix_scores[-1] + hotspot.score)

        next_states = dict(states)
        parents: dict[tuple[int, int], int] = {}
        for (used_threads, used_hotspots), current_score in states.items():
            if used_threads >= thread_limit:
                continue
            max_allocation = min(
                len(thread.hotspots),
                hotspot_limit - used_hotspots,
            )
            for allocation in range(1, max_allocation + 1):
                next_key = (
                    used_threads + 1,
                    used_hotspots + allocation,
                )
                score = current_score + prefix_scores[allocation]
                if score > next_states.get(next_key, float("-inf")):
                    next_states[next_key] = score
                    parents[next_key] = allocation
        states = next_states
        parent_layers.append(parents)

    best_threads = 0
    best_hotspots = 0
    best_score = float("-inf")
    for (used_threads, used_hotspots), score in states.items():
        if used_threads == 0 or used_hotspots == 0:
            continue
        if (
            score > best_score
            or (
                score == best_score
                and (used_threads, used_hotspots)
                > (best_threads, best_hotspots)
            )
        ):
            best_score = score
            best_threads = used_threads
            best_hotspots = used_hotspots

    allocations = [0] * len(ranked_threads)
    used_threads = best_threads
    used_hotspots = best_hotspots
    for index in range(len(ranked_threads) - 1, -1, -1):
        key = (used_threads, used_hotspots)
        allocation = parent_layers[index].get(key)
        if allocation is None:
            continue
        allocations[index] = allocation
        used_threads -= 1
        used_hotspots -= allocation

    selected_threads = [
        thread
        for thread, allocation in zip(ranked_threads, allocations)
        if allocation > 0
    ]
    selected_hotspots = [
        hotspot
        for thread, allocation in zip(ranked_threads, allocations)
        for hotspot in thread.hotspots[:allocation]
    ]

    selected_hotspots.sort(
        key=lambda hotspot: hotspot.global_share,
        reverse=True,
    )
    return selected_threads, selected_hotspots


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n\n[摘要已截断，以控制 LLM 上下文长度]"
    return text[: max(0, max_chars - len(suffix))] + suffix


def summarize_spark_profile(
    profile: Mapping[str, Any],
    max_hotspots: int = DEFAULT_MAX_HOTSPOTS,
    max_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    max_threads: int = DEFAULT_MAX_THREADS,
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

    thread_summaries: list[ThreadHotspotSummary] = []
    total_sample_score = 0.0
    threads = profile.get("threads")
    if not isinstance(threads, list):
        threads = []
    if len(threads) > MAX_PROFILE_THREADS:
        raise SparkFetchError(
            "Spark profile 线程数超过限制："
            f"{len(threads)} > {MAX_PROFILE_THREADS}"
        )
    for thread_index, raw_thread in enumerate(threads):
        if not isinstance(raw_thread, dict):
            continue
        thread_hotspots, thread_total = _collect_thread_hotspots(
            raw_thread,
            class_sources,
        )
        total_sample_score += thread_total
        thread_name = str(raw_thread.get("name") or "unknown thread")
        thread_summaries.append(
            ThreadHotspotSummary(
                name=thread_name,
                total=thread_total,
                hotspots=tuple(
                    Hotspot(
                        path=item.path,
                        score=item.score,
                        share=item.share,
                        source=item.source,
                        thread_name=thread_name,
                        thread_index=thread_index,
                        source_inferred=item.source_inferred,
                        context_count=item.context_count,
                        source_candidates=item.source_candidates,
                    )
                    for item in sorted(
                        thread_hotspots,
                        key=lambda item: item.score,
                        reverse=True,
                    )
                ),
                thread_index=thread_index,
            )
        )

    all_hotspots: list[Hotspot] = []
    normalized_thread_summaries: list[ThreadHotspotSummary] = []
    for thread in thread_summaries:
        normalized_hotspots = tuple(
            Hotspot(
                path=item.path,
                score=item.score,
                share=item.share,
                source=item.source,
                global_share=(
                    item.score / total_sample_score * 100
                    if total_sample_score > 0
                    else 0.0
                ),
                thread_name=item.thread_name,
                thread_index=item.thread_index,
                source_inferred=item.source_inferred,
                context_count=item.context_count,
                source_candidates=item.source_candidates,
            )
            for item in thread.hotspots
        )
        normalized_thread_summaries.append(
            ThreadHotspotSummary(
                name=thread.name,
                total=thread.total,
                hotspots=normalized_hotspots,
                thread_index=thread.thread_index,
            )
        )
        all_hotspots.extend(normalized_hotspots)

    selected_threads, selected_hotspots = _select_representative_hotspots(
        normalized_thread_summaries,
        max_threads=max_threads,
        max_hotspots=max_hotspots,
    )
    total_self_score = sum(item.score for item in all_hotspots)
    selected_score = sum(item.score for item in selected_hotspots)

    lines.append(
        "线程贡献与裁剪（按全局采样占比排序）："
    )
    selected_thread_indices = {
        thread.thread_index for thread in selected_threads
    }
    full_thread_by_index = {
        thread.thread_index: thread for thread in normalized_thread_summaries
    }
    selected_quota: dict[int, int] = {}
    for hotspot in selected_hotspots:
        selected_quota[hotspot.thread_index] = (
            selected_quota.get(hotspot.thread_index, 0) + 1
        )
    for thread in selected_threads:
        contribution = (
            thread.total / total_sample_score * 100
            if total_sample_score > 0
            else 0.0
        )
        full_thread = full_thread_by_index.get(thread.thread_index, thread)
        thread_self_score = sum(item.score for item in full_thread.hotspots)
        thread_self_coverage = (
            thread_self_score / thread.total * 100
            if thread.total > 0
            else 0.0
        )
        lines.append(
            f"- {thread.name}: 全局线程采样占比={_format_percent(contribution)}, "
            f"可解释自耗={_format_percent(thread_self_coverage)}, "
            f"热点配额={selected_quota.get(thread.thread_index, 0)}"
        )
    omitted_threads = [
        thread
        for thread in normalized_thread_summaries
        if thread.thread_index not in selected_thread_indices
    ]
    if omitted_threads:
        omitted_score = sum(thread.total for thread in omitted_threads)
        omitted_names = ", ".join(thread.name for thread in omitted_threads[:8])
        if len(omitted_threads) > 8:
            omitted_names += ", ..."
        lines.append(
            f"- 其余 {len(omitted_threads)} 个线程未展开："
            f"合计占全局采样 {_format_percent(omitted_score / total_sample_score * 100) if total_sample_score > 0 else '0.00%'}；"
            f"线程={omitted_names}"
        )
    explainable_coverage = (
        total_self_score / total_sample_score * 100
        if total_sample_score > 0
        else 0.0
    )
    selected_self_coverage = (
        selected_score / total_self_score * 100
        if total_self_score > 0
        else 0.0
    )
    selected_root_coverage = (
        selected_score / total_sample_score * 100
        if total_sample_score > 0
        else 0.0
    )
    uncovered_root_score = max(total_sample_score - total_self_score, 0.0)
    lines.append(
        f"- 调用图可解释自耗覆盖线程根采样："
        f"{_format_percent(explainable_coverage)}"
        f"（自耗合计={_format_number(total_self_score)}，"
        f"根采样={_format_number(total_sample_score)}）"
    )
    lines.append(
        f"- 调用图未归属/未覆盖线程根采样："
        f"{_format_percent(uncovered_root_score / total_sample_score * 100) if total_sample_score > 0 else '0.00%'}"
    )
    overcounted_root_score = max(total_self_score - total_sample_score, 0.0)
    if overcounted_root_score > 0:
        lines.append(
            f"- 调用图自耗合计超过线程根采样："
            f"{_format_percent(overcounted_root_score / total_sample_score * 100) if total_sample_score > 0 else '0.00%'}"
            "；可能存在共享节点或 profile 数据不一致，相关百分比仅供参考"
        )
    lines.append(
        f"- 已展开热点覆盖可解释自耗："
        f"{_format_percent(selected_self_coverage)}；"
        f"覆盖全线程根采样：{_format_percent(selected_root_coverage)}"
    )
    lines.append("")

    lines.append(
        "代表性采样自耗热点（按全局采样占比排序；每个调用树节点只计一次）："
    )
    for index, hotspot in enumerate(selected_hotspots, start=1):
        source_text = ""
        if hotspot.source:
            inferred_text = "（调用链推断）" if hotspot.source_inferred else ""
            source_text = f"，source={hotspot.source}{inferred_text}"
        elif hotspot.source_candidates:
            candidates = "、".join(hotspot.source_candidates)
            source_text = (
                f"，source=多个调用链候选（{candidates}，归属不确定）"
            )
        context_text = (
            f"，共享调用上下文={hotspot.context_count}"
            if hotspot.context_count > 1
            else ""
        )
        lines.append(
            f"{index}. 全局={_format_percent(hotspot.global_share)}，"
            f"线程内={_format_percent(hotspot.share)} "
            f"({_format_number(hotspot.score)}): "
            f"[{hotspot.thread_name}] {hotspot.path}{source_text}{context_text}"
        )
    if not selected_hotspots:
        lines.append("- 未找到可展开的线程采样树")
    lines.append("")

    third_party_scores: dict[str, float] = {}
    third_party_inferred_scores: dict[str, float] = {}
    ambiguous_source_score = 0.0
    for hotspot in all_hotspots:
        source = hotspot.source.strip()
        if hotspot.source_candidates:
            ambiguous_source_score += hotspot.score
            continue
        if source and not _is_core_source(source):
            third_party_scores[source] = (
                third_party_scores.get(source, 0.0) + hotspot.score
            )
            if hotspot.source_inferred:
                third_party_inferred_scores[source] = (
                    third_party_inferred_scores.get(source, 0.0) + hotspot.score
                )
    lines.append("第三方 source 自耗热点聚合：")
    if third_party_scores and total_sample_score > 0:
        for index, (source, score) in enumerate(
            sorted(third_party_scores.items(), key=lambda item: item[1], reverse=True)[
                :max_hotspots
            ],
            start=1,
        ):
            inferred_score = third_party_inferred_scores.get(source, 0.0)
            inferred_text = (
                f"，其中调用链推断占该 source 自耗="
                f"{_format_percent(inferred_score / score * 100)}"
                if inferred_score > 0
                else ""
            )
            lines.append(
                f"{index}. {source}: {_format_percent(score / total_sample_score * 100)} "
                f"({_format_number(score)}){inferred_text}"
            )
    else:
        lines.append("- 未找到可归属的第三方 source")
    if ambiguous_source_score > 0:
        lines.append(
            f"- 多 source 调用链归属不确定："
            f"{_format_percent(ambiguous_source_score / total_sample_score * 100) if total_sample_score > 0 else '0.00%'}"
            "（未计入单一第三方 source 聚合）"
        )
    lines.append(
        "- source 归属说明：优先使用热点节点自身的 source；叶节点未标注时，"
        "沿代表性调用链使用最近的非内置 source；多个候选时标记为归属不确定。"
    )

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
    - 标记为“调用链推断”的 source 只能作为归属线索，不要当作直接证据；
      结合完整调用路径和采样占比判断。
    - 标记为“归属不确定”的多个 source 候选不能强行归因给任一 Mod。
    - “共享调用上下文”表示同一调用树节点被多个父路径引用，自耗已去重，
      不要将它重复相加。
    - 注意“调用图可解释自耗覆盖”和“已展开热点覆盖”两个指标，
      未覆盖部分不能被当作不存在性能开销。
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


def _normalize_responses_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.lower().endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def _object_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _extract_responses_text(response: object, provider_name: str) -> str:
    output_text = _object_value(response, "output_text")
    text = str(output_text or "").strip()
    if text:
        return text

    output = _object_value(response, "output")
    chunks: list[str] = []
    if isinstance(output, list):
        for output_item in output:
            content = _object_value(output_item, "content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                content_text = _object_value(content_item, "text")
                if content_text:
                    chunks.append(str(content_text))

    text = "\n".join(chunks).strip()
    if not text:
        raise ValueError(f"{provider_name} 返回内容为空")
    return text


async def _collect_responses_stream(stream: object, provider_name: str) -> str:
    chunks: list[str] = []
    completed_response: object = None
    try:
        async for event in stream:
            event_type = _object_value(event, "type")
            if event_type == "response.output_text.delta":
                delta = _object_value(event, "delta")
                if delta:
                    chunks.append(str(delta))
            elif event_type == "response.completed":
                completed_response = _object_value(event, "response")
            elif event_type == "error":
                message = _object_value(event, "message") or "流式响应失败"
                raise ValueError(f"{provider_name}：{message}")
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            await close()

    text = "".join(chunks).strip()
    if text:
        return text
    if completed_response is not None:
        return _extract_responses_text(completed_response, provider_name)
    raise ValueError(f"{provider_name} 返回内容为空")


async def _call_openai_compatible(
    client: httpx.AsyncClient,
    prompt: str,
    provider: Mapping[str, Any],
    config: SparkAnalyzeConfig,
) -> str:
    config = SparkAnalyzeConfig.from_object(config)
    provider_name = str(provider.get("name") or "OpenAI 兼容 Provider")
    api_key = str(provider.get("api_key") or "").strip()
    base_url = str(provider.get("base_url") or "").strip()
    model = str(provider.get("model") or "").strip() or "gpt-4o"
    if not api_key or not base_url:
        raise ValueError(f"{provider_name} 缺少 api_key 或 base_url")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": config.llm_max_tokens,
    }
    if config.reasoning_effort:
        payload["reasoning_effort"] = config.reasoning_effort

    response = await client.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=config.llm_timeout_seconds,
    )
    response.raise_for_status()
    response_data = response.json()
    text = _extract_provider_text(response_data, provider_name)
    return text


async def _call_responses_api(
    prompt: str,
    provider: Mapping[str, Any],
    config: SparkAnalyzeConfig,
) -> str:
    config = SparkAnalyzeConfig.from_object(config)
    provider_name = str(provider.get("name") or "Responses API Provider")
    api_key = str(provider.get("api_key") or "").strip()
    configured_base_url = str(provider.get("base_url") or "").strip()
    model = str(provider.get("model") or "").strip() or "gpt-4o"
    if not api_key or not configured_base_url:
        raise ValueError(f"{provider_name} 缺少 api_key 或 base_url")

    request_kwargs: dict[str, Any] = {
        "model": model,
        "input": prompt,
        "max_output_tokens": config.llm_max_tokens,
        "stream": True,
    }
    if config.reasoning_effort:
        request_kwargs["reasoning"] = {"effort": config.reasoning_effort}

    sdk_client: AsyncOpenAI | None = None
    try:
        sdk_client = AsyncOpenAI(
            api_key=api_key,
            base_url=_normalize_responses_base_url(configured_base_url),
            timeout=config.llm_timeout_seconds,
            # A connection drop can happen after the upstream already charged
            # the request. Do not let the SDK retry and consume tokens again.
            max_retries=0,
        )
        stream = await sdk_client.responses.create(**request_kwargs)
        return await _collect_responses_stream(stream, provider_name)
    finally:
        if sdk_client is not None:
            await sdk_client.close()


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
    config: SparkAnalyzeConfig,
    client: httpx.AsyncClient,
) -> str:
    config = SparkAnalyzeConfig.from_object(config)
    providers = list(config.llm_providers)
    if not providers:
        providers = [
            {
                "__template_key": "astrbot_provider",
                "name": "AstrBot Provider",
            }
        ]
        logger.debug("[SparkAnalyze] 未配置 Provider 列表，使用当前 AstrBot Provider")

    last_error: Exception | None = None
    for provider in providers:
        if not isinstance(provider, Mapping):
            last_error = ValueError("Provider 配置不是对象")
            continue

        provider_name = str(provider.get("name") or "未命名 Provider")
        template = _provider_template(provider)
        try:
            logger.debug(
                "[SparkAnalyze] 尝试 Provider：name=%s, template=%s",
                provider_name,
                template,
            )
            if template == "astrbot_provider":
                text = await _call_astrbot_provider(context, event, prompt)
            elif template == RESPONSES_API_TEMPLATE:
                text = await _call_responses_api(prompt, provider, config)
            elif template in OPENAI_COMPATIBLE_TEMPLATES:
                text = await _call_openai_compatible(
                    client,
                    prompt,
                    provider,
                    config,
                )
            else:
                raise ValueError(f"不支持的 Provider 模板：{template or '未知'}")
            if config.debug_log_llm_response:
                logger.debug("[SparkAnalyze] %s 返回：%s", provider_name, text)
            logger.debug("[SparkAnalyze] Provider 分析成功：%s", provider_name)
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
        self.config: SparkAnalyzeConfig = SparkAnalyzeConfig.from_object(config)
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()
        self._in_flight_codes: set[str] = set()
        self._in_flight_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._analysis_semaphore = asyncio.Semaphore(
            self.config.max_concurrent_analyses
        )
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
            if not is_group_allowed(group_id, self.config.enabled_group_ids):
                return

            messages = _get_message_segments(event)
            if len(messages) != 1 or not isinstance(messages[0], Comp.Plain):
                return

            link = extract_spark_profile_link(getattr(messages[0], "text", ""))
            if link is None:
                return

            logger.debug(
                "[SparkAnalyze] 识别到 Spark profile 链接：url=%s, code=%s, group_id=%s",
                link.url,
                link.code,
                group_id,
            )
            event.stop_event()
            await _react_to_parsed_message(event)

            if not await self._claim_code(link.code):
                logger.debug(
                    "[SparkAnalyze] code 已在分析中，跳过重复任务：code=%s",
                    link.code,
                )
                return

            analysis_slot_acquired = False
            try:
                await self._analysis_semaphore.acquire()
                analysis_slot_acquired = True
                client = await self._get_http_client()
                raw_profile = await fetch_spark_profile(
                    link.code,
                    max_bytes=self.config.max_profile_bytes,
                    timeout_seconds=self.config.request_timeout_seconds,
                    client=client,
                )
                if not raw_profile:
                    raise SparkFetchError("Spark profile 响应为空")
                logger.debug(
                    "[SparkAnalyze] 已下载 Spark profile：code=%s, bytes=%s",
                    link.code,
                    len(raw_profile),
                )
                profile = await fetch_spark_json(
                    link.code,
                    max_bytes=self.config.max_json_bytes,
                    timeout_seconds=self.config.request_timeout_seconds,
                    client=client,
                )
                summary = await asyncio.to_thread(
                    summarize_spark_profile,
                    profile,
                    max_hotspots=self.config.max_hotspots,
                    max_chars=self.config.max_summary_chars,
                    max_threads=self.config.max_threads,
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
                if analysis_slot_acquired:
                    self._analysis_semaphore.release()
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
