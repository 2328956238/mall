"""
Agent API 路由
==============
替代原有的 chatbot 接口，使用 ReAct Agent 进行多步推理对话。

核心逻辑：
  1. POST /api/v1/agent/message → Agent 对话（有推理能力）
  2. 返回推理轨迹（thoughts），前端可展示推理过程

与旧 chat 接口的关系：
  - agent/message 是升级版，走 ReAct 推理
  - chat/message 保留作为降级（当 Agent 服务不可用时）
"""

from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas.chat import AgentChatRequest, AgentChatResponse, AgentThoughtSchema
from app.schemas.common import APIResponse
from app.dependencies import get_current_user
from app.db.redis_client import get_redis
from app.db.session import get_db
from app.models.user import User
from app.agent.factory import create_agent

router = APIRouter(prefix="/agent", tags=["AI Agent（智能推理）"])


@router.post("/message", response_model=APIResponse[AgentChatResponse])
async def agent_message(
    request: AgentChatRequest,
    current_user: User = Depends(get_current_user),
    redis=Depends(get_redis),
    db=Depends(get_db),
):
    """
    ReAct Agent 对话接口（升级版智能客服）

    与旧版 chat/message 的区别：
      - 具备多步推理能力（Thought → Action → Observation → ... → Final Answer）
      - 可以主动调用工具：查询商品、查询订单、查询秒杀活动、执行秒杀下单
      - 返回推理轨迹（thoughts），展示 AI 的思考过程

    Args:
        request (AgentChatRequest):
            - message (str): 用户消息
            - history (list, optional): 历史对话记录

    Returns:
        APIResponse[AgentChatResponse]:
            - reply (str): Agent 最终回复
            - thoughts (list): 推理步骤链（每步的思考/工具/观察结果）
            - timestamp (datetime): 响应时间
    """
    try:
        # 将历史消息转为统一格式
        history_dicts = [
            {"role": msg.role, "content": msg.content}
            for msg in (request.history or [])
        ]

        # 创建 Agent（注入当前用户 ID 用于下单和订单校验）
        agent = create_agent(
            redis=redis,
            db=db,
            current_user_id=str(current_user.id),
        )

        # 执行推理（传入 user_id 用于上下文注入和链路追踪）
        reply, thoughts = await agent.reason(
            user_message=request.message,
            history=history_dicts,
            user_id=str(current_user.id),
        )

        # 转换为 schema
        thought_schemas = [
            AgentThoughtSchema(
                step=t.step,
                thought=t.thought,
                action=t.action,
                observation=t.observation,
                tool_latency_ms=round(t.tool_latency_ms, 1) if t.tool_latency_ms else None,
            )
            for t in thoughts
        ]

        return APIResponse(
            data=AgentChatResponse(
                reply=reply,
                thoughts=thought_schemas,
            ),
            message="success",
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 服务异常: {str(e)}",
        )