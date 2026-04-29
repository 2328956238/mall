"""
Agent 推理链路追踪
==================
在已有的 Prometheus + Jaeger 基础上，为 Agent 推理链路添加可观测性。

Span 结构（嵌套在 HTTP 请求 span 之下）：
```
POST /api/v1/agent/message  (HTTP span - 由 FastAPIInstrumentor 自动创建)
  └── agent.reasoning        (Agent 总推理 span)
        ├── agent.intent.detect    (意图分类)
        ├── agent.reasoning.step.1 (第1轮推理)
        │     ├── llm.call         (LLM 调用)
        │     └── agent.tool.call  (工具调用，如有)
        ├── agent.reasoning.step.2 (第2轮推理)
        │     ├── llm.call
        │     └── agent.tool.call
        └── ... (最多 MAX_REASONING_STEPS 轮)
```

Prometheus 自定义指标（由 prometheus.py 的 Instrumentator 注册）：
  - agent_reasoning_rounds: Histogram - 推理轮次分布
  - agent_tool_latency_ms: Histogram - 工具调用延迟
  - agent_tool_calls_total: Counter - 工具调用计数（按工具名标签）
  - agent_intent_total: Counter - 意图分类计数（按意图标签）

使用方式：
    from app.agent.tracing import AgentTracer

    tracer = AgentTracer()
    with tracer.reasoning_span(user_id, user_message) as span:
        intent = tracer.detect_intent(user_message)
        with tracer.step_span(step_num, thought, action) as step_span:
            observation = await execute_tool(...)
"""

import logging
import time
from typing import Optional, Any
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

# 获取 Tracer
# 与 app/middleware/trace.py 共享同一个 TracerProvider
_TRACER = trace.get_tracer(__name__)


class AgentTracer:
    """
    Agent 推理链路追踪器。

    提供上下文管理器风格的 span 创建，确保 span 正确关闭和异常记录。

    与现有可观测性栈的关系：
      - Jaeger: Agent 推理链路作为 HTTP span 的子 span 出现
      - Prometheus: 通过 app/middleware/prometheus.py 的 Instrumentator 暴露指标
      - 日志: 通过 Python logging 记录推理步骤（已有 logger）
    """

    def __init__(self):
        self._tracer = _TRACER

    # ── 推理总 Span ──

    def reasoning_span(
        self,
        user_id: Optional[str] = None,
        message_preview: Optional[str] = None,
    ) -> Span:
        """
        创建一个 Agent 总推理 span。

        Args:
            user_id: 当前用户 ID
            message_preview: 用户消息的前 50 个字符（避免记录敏感信息）

        Returns:
            Span 上下文管理器
        """
        attributes = {}
        if user_id:
            attributes["agent.user_id"] = user_id
        if message_preview:
            attributes["agent.message_preview"] = message_preview[:50]

        span = self._tracer.start_span(
            name="agent.reasoning",
            kind=trace.SpanKind.INTERNAL,
            attributes=attributes,
        )
        return span

    # ── 意图检测 Span ──

    def detect_intent(self, intent: str) -> None:
        """
        记录意图分类结果到当前活跃 span。

        这是轻量级操作，不需要创建子 span，直接在当前 span 上添加属性。
        """
        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            current_span.set_attribute("agent.intent", intent)

    # ── 单轮推理 Span ──

    def step_span(
        self,
        step_number: int,
        thought: Optional[str] = None,
        tool_name: Optional[str] = None,
    ) -> Span:
        """
        创建一个推理步骤的子 span。

        Args:
            step_number: 推理轮次序号
            thought: 当前步骤的思考内容（截断到 200 字符）
            tool_name: 调用的工具名（如有）

        Returns:
            Span 上下文管理器
        """
        attributes = {
            "agent.step": step_number,
        }
        if thought:
            attributes["agent.thought"] = thought[:200]
        if tool_name:
            attributes["agent.tool.name"] = tool_name

        span = self._tracer.start_span(
            name=f"agent.reasoning.step.{step_number}",
            kind=trace.SpanKind.INTERNAL,
            attributes=attributes,
        )
        return span

    # ── LLM 调用 Span ──

    def llm_span(self, model: str) -> Span:
        """
        创建 LLM 调用 span。

        记录模型名称和调用的 token 消耗（如能从响应中获取）。

        Args:
            model: LLM 模型名称

        Returns:
            Span 上下文管理器
        """
        span = self._tracer.start_span(
            name="llm.call",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "llm.model": model,
                "llm.vendor": "scnet",
            },
        )
        return span

    # ── 工具调用 Span ──

    def tool_span(self, tool_name: str, params: dict) -> Span:
        """
        创建工具调用 span。

        记录工具名、参数和调用延迟。
        延迟会在 span 关闭时由调用方设置。

        Args:
            tool_name: 工具名
            params: 调用参数

        Returns:
            Span 上下文管理器
        """
        span = self._tracer.start_span(
            name=f"agent.tool.{tool_name}",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "agent.tool.name": tool_name,
                "agent.tool.params": str(params)[:200],
            },
        )
        return span

    # ── Span 辅助方法 ──

    @staticmethod
    def end_span(span: Span, status: StatusCode = StatusCode.OK, description: Optional[str] = None):
        """
        安全关闭 span 并设置状态。

        Args:
            span: 要关闭的 span
            status: 状态码（OK / ERROR / UNSET）
            description: 状态描述（错误时记录错误信息）
        """
        span.set_status(Status(status, description or ""))
        span.end()

    @staticmethod
    def record_exception(span: Span, exception: Exception):
        """在 span 上记录异常信息"""
        span.record_exception(exception)
        span.set_status(Status(StatusCode.ERROR, str(exception)))

    # ── 工具调用耗时记录 ──

    @staticmethod
    def record_tool_latency(span: Span, latency_ms: float):
        """在工具调用 span 上记录耗时"""
        span.set_attribute("agent.tool.latency_ms", latency_ms)