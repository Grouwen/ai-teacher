"""LLM 调用层（08 表选型）：deepseek-chat 主 / deepseek-reasoner 备。

- 结构化输出：schema_output strict 不可用 → 降级 json_object + 代码校验（12 表 B11）。
- 失败重试 ≤2 次指数退避；超时：规划/出题 60s、批改 30s（06 表 C15、10 表 I 列）。
- 每次调用写 trace（06 表 C18）。
"""
import json
import re
import time

from openai import OpenAI

import config
import tracer

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.API_KEY or "missing", base_url=config.BASE_URL)
    return _client


class LLMError(RuntimeError):
    """主备模型均失败或输出不可修复时抛出，由上层转友好提示。"""


def chat(stage: str, messages: list, temperature: float = 0.7,
         response_format: dict | None = None, timeout: int | None = None,
         trace_id: str = "-") -> str:
    """单步对话补全。主模型失败自动切备用模型，每模型重试 ≤LLM_RETRIES 次（指数退避）。"""
    if not config.API_KEY:
        raise LLMError("未配置 OPENAI_API_KEY 环境变量，无法调用模型。请先设置后重启。")
    timeout = timeout or config.LLM_TIMEOUT
    last_err = None
    for model in (config.MODEL_MAIN, config.MODEL_BACKUP):
        for attempt in range(config.LLM_RETRIES + 1):  # 首次 + 重试 2 次
            t0 = time.time()
            try:
                kwargs = dict(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=config.MAX_TOKENS,
                    timeout=timeout,
                )
                if response_format:
                    kwargs["response_format"] = response_format
                resp = _get_client().chat.completions.create(**kwargs)
                usage = getattr(resp, "usage", None)
                tracer.log_event(
                    trace_id, "llm", stage=stage, model=model, attempt=attempt,
                    latency_ms=int((time.time() - t0) * 1000),
                    prompt_chars=sum(len(m.get("content") or "") for m in messages),
                    total_tokens=getattr(usage, "total_tokens", None),
                )
                return resp.choices[0].message.content
            except Exception as e:  # noqa: BLE001 网络/限流/模型不可用统一重试
                last_err = e
                tracer.log_event(trace_id, "llm_error", stage=stage, model=model,
                                 attempt=attempt, error=str(e)[:300])
                if attempt < config.LLM_RETRIES:
                    time.sleep(2 ** attempt)  # 指数退避
    raise LLMError(f"模型调用失败（主备均已重试）：{last_err}")


def _parse_json(text: str) -> dict:
    """从模型输出解析 JSON：容忍代码围栏与前后杂文（降级 json_object 后的代码校验前置）。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("输出中找不到 JSON 对象")
        return json.loads(match.group(0))


def structured(stage: str, messages: list, schema_name: str, schema: dict,
               validator, temperature: float = 0.7, timeout: int | None = None,
               trace_id: str = "-") -> dict:
    """结构化生成（12 表 B11/B14）：
    1. 先走 schema_output（JSON Schema strict）；
    2. 接口不支持则降级 json_object + validator 代码校验；
    3. 校验不过自动重试 1 次，仍不过抛 LLMError，不出残缺结果。
    """
    formats = (
        {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
        {"type": "json_object"},
    )
    last_err: Exception | None = None
    for fmt in formats:
        msgs = list(messages)
        text = ""
        for attempt in range(2):  # 自检不过重试 1 次
            try:
                text = chat(stage, msgs, temperature=temperature, timeout=timeout,
                            response_format=fmt, trace_id=trace_id)
                data = _parse_json(text)
                return validator(data)
            except LLMError:
                last_err = None
                break  # 该 response_format 整体不可用（如 strict 不支持），换降级格式
            except (ValueError, KeyError, TypeError) as e:
                last_err = e
                tracer.log_event(trace_id, "validate_fail", stage=stage,
                                 attempt=attempt, error=str(e)[:300])
                msgs = list(messages) + [
                    {"role": "assistant", "content": text},
                    {"role": "user",
                     "content": f"上次输出未通过校验：{e}。请修正后重新输出完整 JSON，不要输出任何解释文字。"},
                ]
        else:
            raise LLMError(f"{stage} 输出两次校验均未通过：{last_err}")
    raise LLMError(f"{stage} 结构化输出不可用：{last_err}")
