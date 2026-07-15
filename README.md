# Spark 性能分析

这是一个 AstrBot 插件，用于自动分析 Minecraft 的 Spark profile 链接。

在启用的群聊中发送类似下面的链接：

```text
https://spark.lucko.me/02pGFymGbD
```

插件会立即记录日志、给原消息贴表情并拦截后续处理，然后：

1. 从 Spark bytebin 下载并校验原始 `sparkprofile`。
2. 从官方 JSON service 获取完整采样数据。
3. 提取平台信息、TPS/MSPT、内存、Mod/source、线程自耗热点和调用路径；调用树中的共享节点只统计一次，避免祖先/子节点重复计数。
4. 使用配置的 LLM Provider 生成中文性能诊断。
5. 通过合并转发回复来源群。

失败时只记录日志，不在群内发送错误提示。

## 配置

- `enabled_group_ids`：启用自动分析的群号列表，留空表示不处理任何群。
- `llm_providers`：按顺序尝试的 Provider 列表，支持：
  - `openai_compatible`
  - `astrbot_provider`
  - `modelscope`
  - `responses_api`：使用官方 `openai-python` SDK 的 Responses API。
- `llm_max_tokens`：LLM 输出 token 上限，默认 `4096`。
- `llm_timeout_seconds`：LLM 请求超时，默认 `120` 秒。
- `reasoning_effort`：可选的 `reasoning_effort` 参数。
- `debug_log_llm_response`：是否记录 LLM 返回文本。
- `max_profile_bytes`：原始 profile 大小上限，默认 20 MiB。
- `max_json_bytes`：完整 JSON 大小上限，默认 10 MiB。
- `max_summary_chars`：发送给 LLM 的摘要字符上限，默认 60000。
- `max_hotspots`：保留的热点数量，默认 20。
- `max_threads`：按线程根节点总采样值选取并展开的高占用线程数量，默认 8；无可用自耗热点的线程不会占用名额，热点名额按线程采样占比加权分配。
- `request_timeout_seconds`：Spark 请求超时，默认 60 秒。

当 `llm_providers` 留空时，插件使用当前会话的 AstrBot Provider。配置多个 Provider 时，插件会按列表顺序尝试，第一个成功的结果会被采用。

## 识别规则

- 只监听 `enabled_group_ids` 中的群聊。
- 只处理单条纯文本消息中的一个 `https://spark.lucko.me/<code>` 链接。
- 不处理其他域名、混合文件/图片消息、转发消息或多个 Spark 链接。
- v0.1.0 只支持 `sparkprofile`，不支持 Spark heap 或 health 数据。
- 识别成功后会调用 OneBot 的 `set_msg_emoji_like`，使用表情 ID `289`、类型 `"1"`；平台不支持时不影响分析流程。

## 开发与验证

```bash
python -m py_compile main.py
python -m json.tool _conf_schema.json
python -m unittest discover -s tests -v
```

## 依赖

```bash
pip install -r requirements.txt
```

`responses_api` 的 `base_url` 可填写 `https://api.openai.com` 或带 `/v1`
的地址；插件会按 SDK 要求规范化，并将 `reasoning_effort` 映射为
Responses API 的 `reasoning.effort`。插件默认使用流式响应以降低长请求的网关
超时风险，并关闭了 `openai-python` 的自动重试，避免上游已扣费但响应断开时重复消费。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
