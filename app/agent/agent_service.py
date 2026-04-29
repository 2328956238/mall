"""
ReAct Agent 核心实现
====================
从 chatbot → 多步推理 Agent 的跃迁。

架构：
  1. 工具层：4 个领域工具（商品/订单/秒杀/下单）
  2. 推理层：ReAct 循环（Thought → Action → Observation → ... → Final Answer）
  3. Prompt 层：3 层模板体系（TemplateLoader 加载 YAML 模板）
  4. 观测层：OpenTelemetry 推理链路追踪（AgentTracer）

ReAct 格式：
  Thought: 当前推理步骤的思考
  Action: {"tool": "tool_name", "params": {...}}
  Observation: 工具返回的结果
  ...（循环直至给出 Final Answer）
  Final Answer: 对用户的最终回复
"""

import json
import logging
import re
import time
from typing import Any, Optional
from datetime import datetime
from opentelemetry.trace import StatusCode

from app.config import settings
from app.services.template_loader import TemplateLoader
from app.agent.tracing import AgentTracer
from app.agent.context_builder import ContextBuilder

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 1. 工具定义
# ──────────────────────────────────────────────


class AgentTool:
    """
    一个可被 Agent 调用的工具。

    每个工具包含：
    - name: 工具名，LLM 通过此名调用
    - description: 描述，LLM 据此判断何时使用
    - parameters: JSON Schema 格式的参数定义
    - handler: 实际执行的异步函数
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: callable,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def to_function_spec(self) -> dict[str, Any]:
        """转换为 OpenAI Function Calling 格式（兼容通用 LLM）"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs) -> Any:
        """执行工具并返回结果"""
        try:
            result = await self.handler(**kwargs)
            return result
        except Exception as e:
            logger.error(f"Tool [{self.name}] execution failed: {e}", exc_info=True)
            return {"error": f"工具调用失败: {str(e)}"}


# ──────────────────────────────────────────────
# 2. 推理过程中间状态
# ──────────────────────────────────────────────


class AgentThought:
    """
    ReAct 推理过程中的一个步骤。
    用于链路追踪和调试。后续可观测性扩展的基础数据结构。
    """

    def __init__(self, step: int, thought: str, action: Optional[dict] = None):
        self.step = step
        self.thought = thought
        self.action = action  # {"tool": "...", "params": {...}}
        self.observation: Optional[str] = None
        self.tool_latency_ms: Optional[float] = None

    def set_observation(self, observation: str, latency_ms: float):
        self.observation = observation
        self.tool_latency_ms = latency_ms

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "thought": self.thought,
            "action": self.action,
            "observation": self.observation,
            "tool_latency_ms": self.tool_latency_ms,
        }


# ──────────────────────────────────────────────
# 3. Agent 主服务（ReAct 推理循环）
# ──────────────────────────────────────────────


class AgentService:
    """
    ReAct Agent 主服务。

    核心流程：
      1. 接收用户消息 + 历史上下文
      2. 使用 TemplateLoader 构建 3 层 system prompt（Layer1 Meta + Layer2 Task + Layer3 Few-shot）
      3. 调用 LLM → 解析回复（Thought / Action / Final Answer）
      4. 如果是 Action → 执行工具 → 将 Observation 加入对话 → 回到步骤 3
      5. 如果是 Final Answer → 返回给用户

    安全约束：
      - 最大推理轮次：MAX_REASONING_STEPS（防止无限循环）
      - 工具调用超时：tool 自带超时（由 httpx timeout 控制）
      - 参数校验：由 tool handler 内部校验
    """

    # 最大推理轮次，防止 Agent 无限循环
    MAX_REASONING_STEPS = 6

    def __init__(self, redis=None, db=None):
        self.api_key = settings.SC_NET_API_KEY
        self.api_base = settings.SC_NET_API_BASE
        self.model = settings.SC_NET_MODEL
        self._tools: dict[str, AgentTool] = {}
        self._tracer = AgentTracer()
        self._redis = redis
        self._db = db

    # ── 工具注册 ──

    def register_tool(self, tool: AgentTool):
        """注册一个工具到 Agent"""
        self._tools[tool.name] = tool
        logger.info(f"Agent tool registered: {tool.name}")

    def get_tool_specs(self) -> list[dict]:
        """返回所有工具的 Function Calling 格式定义"""
        return [t.to_function_spec() for t in self._tools.values()]

    def _build_tool_descriptions(self) -> str:
        """生成工具描述文本，用于注入到 system prompt"""
        return "\n".join(
            f"- {t.name}: {t.description}，参数：{json.dumps(t.parameters, ensure_ascii=False)}"
            for t in self._tools.values()
        )

    # ── 核心推理（含 OpenTelemetry 追踪） ──

    async def reason(
        self,
        user_message: str,
        history: Optional[list[dict]] = None,
        user_id: Optional[str] = None,
    ) -> tuple[str, list[AgentThought]]:
        """
        ReAct 推理主入口（含链路追踪）。

        Args:
            user_message: 用户当前消息
            history: 可选的历史消息 [{"role": "...", "content": "..."}]
            user_id: 当前用户 ID（用于链路追踪标签）

        Returns:
            (final_reply, thoughts): 最终回复 + 推理步骤列表（用于观测/追踪）
        """
        # ── 创建总推理 Span ──
        reasoning_span = self._tracer.reasoning_span(
            user_id=user_id,
            message_preview=user_message,
        )
        reasoning_span.__enter__()

        try:
            # 意图检测
            loader = TemplateLoader()
            intent = loader.detect_intent(user_message)
            self._tracer.detect_intent(intent)

            tool_descriptions = self._build_tool_descriptions()

            # 构建实时上下文（用户信息 + 系统状态 + 对话历史）
            custom_context = await ContextBuilder.build_context(
                redis=self._redis,
                db=self._db,
                user_id=user_id,
                user_message=user_message,
                history=history,
            )

            system_prompt = loader.build_full_prompt(
                user_message=user_message,
                tool_descriptions=tool_descriptions,
                custom_context=custom_context,
            )

            # 构建消息列表
            messages = [{"role": "system", "content": system_prompt}]

            for msg in (history or []):
                if msg.get("role") in ("user", "assistant"):
                    messages.append(msg)

            messages.append({"role": "user", "content": user_message})

            thoughts: list[AgentThought] = []
            step = 0

            while step < self.MAX_REASONING_STEPS:
                step += 1

                # 创建推理步骤 Span
                step_span = self._tracer.step_span(step_number=step)
                step_span.__enter__()

                try:
                    # LLM 调用（带子 Span）
                    llm_span = self._tracer.llm_span(self.model)
                    llm_span.__enter__()
                    try:
                        llm_reply = await self._call_llm(messages)
                        self._tracer.end_span(llm_span)
                    except Exception as e:
                        self._tracer.record_exception(llm_span, e)
                        self._tracer.end_span(llm_span, StatusCode.ERROR, str(e))
                        raise

                    # 解析回复
                    parsed = self._parse_llm_reply(llm_reply)
                    step_span.set_attribute("agent.step.type", parsed["type"])

                    if parsed["type"] == "final_answer":
                        step_span.set_attribute("agent.step.thought", parsed.get("thought", "")[:200])
                        self._tracer.end_span(step_span)

                        thought = AgentThought(
                            step=step,
                            thought=parsed.get("thought", ""),
                            action=None,
                        )
                        thoughts.append(thought)
                        logger.info(f"Agent reasoning completed in {step} steps")
                        self._tracer.end_span(reasoning_span)
                        return parsed["content"], thoughts

                    elif parsed["type"] == "action":
                        tool_name = parsed["tool_name"]
                        tool_params = parsed["tool_params"]

                        step_span.set_attribute("agent.tool.name", tool_name)
                        step_span.set_attribute("agent.step.thought", parsed.get("thought", "")[:200])

                        thought = AgentThought(
                            step=step,
                            thought=parsed.get("thought", ""),
                            action={"tool": tool_name, "params": tool_params},
                        )

                        # 工具调用（带子 Span）
                        tool_span = self._tracer.tool_span(tool_name, tool_params)
                        tool_span.__enter__()
                        try:
                            t0 = time.monotonic()
                            observation = await self._execute_tool(tool_name, tool_params)
                            latency = (time.monotonic() - t0) * 1000
                            self._tracer.record_tool_latency(tool_span, latency)
                            thought.set_observation(str(observation), latency)
                            self._tracer.end_span(tool_span)
                        except Exception as e:
                            self._tracer.record_exception(tool_span, e)
                            self._tracer.end_span(tool_span, StatusCode.ERROR, str(e))
                            thought.set_observation(f"error: {e}", 0)
                            raise

                        thoughts.append(thought)

                        messages.append({"role": "assistant", "content": llm_reply})
                        messages.append({
                            "role": "user",
                            "content": f"Observation: {json.dumps(observation, ensure_ascii=False)}",
                        })
                        logger.info(
                            f"Step {step}: tool={tool_name} params={tool_params} "
                            f"latency={latency:.0f}ms"
                        )
                        self._tracer.end_span(step_span)

                    else:
                        step_span.set_attribute("agent.step.type", "fallback")
                        self._tracer.end_span(step_span)
                        logger.warning(f"Step {step}: failed to parse LLM reply, treating as final")
                        thought = AgentThought(step=step, thought="", action=None)
                        thoughts.append(thought)
                        self._tracer.end_span(reasoning_span)
                        return llm_reply, thoughts

                except Exception as e:
                    self._tracer.record_exception(step_span, e)
                    self._tracer.end_span(step_span, StatusCode.ERROR, str(e))
                    raise

            # 超过最大轮次
            reasoning_span.set_attribute("agent.exceeded_max_steps", True)
            logger.warning(f"Agent exceeded max reasoning steps ({self.MAX_REASONING_STEPS})")
            self._tracer.end_span(reasoning_span)
            return (
                "抱歉，这个问题比较复杂，我需要更多时间思考。请稍后再试，或联系人工客服。😊",
                thoughts,
            )

        except Exception as e:
            self._tracer.record_exception(reasoning_span, e)
            self._tracer.end_span(reasoning_span, StatusCode.ERROR, str(e))
            logger.error(f"Agent reasoning failed: {e}", exc_info=True)
            return (
                "抱歉，AI 服务出现异常，请稍后再试。😊",
                [],
            )

    def _parse_llm_reply(self, reply: str) -> dict:
        """
        解析 LLM 回复，提取 Action 或 Final Answer。

        支持的格式：
        1. Action 格式：
           Thought: xxx
           Action: {"tool": "xxx", "params": {...}}

        2. Final Answer 格式：
           Thought: xxx
           Final Answer: xxx

        3. 只有 Action 或 Final Answer（无 Thought）
        """
        result = {"type": "unknown", "content": reply}

        # 提取 Final Answer
        fa_match = re.search(r"Final Answer:\s*(.+)", reply, re.DOTALL)
        if fa_match:
            # 也提取可能存在的 Thought
            thought_match = re.search(r"Thought:\s*(.+?)(?=Final Answer:|Action:)", reply, re.DOTALL)
            result = {
                "type": "final_answer",
                "content": fa_match.group(1).strip(),
                "thought": thought_match.group(1).strip() if thought_match else "",
            }
            return result

        # 提取 Action（JSON 格式的工具调用）
        action_match = re.search(r"Action:\s*(\{.+?\})", reply, re.DOTALL)
        if action_match:
            try:
                action_json = json.loads(action_match.group(1))
                tool_name = action_json.get("tool", "")
                tool_params = action_json.get("params", {})

                thought_match = re.search(r"Thought:\s*(.+?)(?=Action:)", reply, re.DOTALL)
                result = {
                    "type": "action",
                    "tool_name": tool_name,
                    "tool_params": tool_params,
                    "thought": thought_match.group(1).strip() if thought_match else "",
                }
                return result
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse Action JSON: {action_match.group(1)}")

        # 如果既没有 Final Answer 也没有 Action，检查是否直接是自然语言回复
        # 此时当作 Final Answer 处理（降级策略）
        if not re.search(r"(Thought:|Action:|Final Answer:)", reply):
            result = {
                "type": "final_answer",
                "content": reply,
                "thought": "直接回复（非标准 ReAct 格式）",
            }
            return result

        return result

    async def _execute_tool(self, tool_name: str, params: dict) -> Any:
        """执行工具调用"""
        tool = self._tools.get(tool_name)
        if not tool:
            return {"error": f"未知工具: {tool_name}，可用工具: {list(self._tools.keys())}"}

        logger.info(f"Executing tool [{tool_name}] with params: {params}")
        return await tool.execute(**params)