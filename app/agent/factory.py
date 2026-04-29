"""
Agent 工厂
==========
组装 AgentService + 4 个领域工具，对外提供统一接口。

使用方式：
    agent = create_agent(redis, db)
    reply, thoughts = await agent.reason("有什么秒杀活动？")
"""

import logging
from typing import Optional
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agent_service import AgentService, AgentTool
from app.agent.tools import (
    _get_product_info,
    _get_order_status,
    _get_seckill_list,
    _place_seckill_order,
)

logger = logging.getLogger(__name__)


def create_agent(redis: Redis, db: AsyncSession, current_user_id: Optional[str] = None) -> AgentService:
    """
    创建并组装一个完整的 ReAct Agent。

    Args:
        redis: Redis 客户端（用于秒杀库存预减和限流）
        db: 数据库会话（用于查询商品/订单/秒杀活动）
        current_user_id: 当前登录用户 ID（用于下单和订单归属校验）

    Returns:
        配置好 4 个工具的 AgentService 实例
    """
    agent = AgentService(redis=redis, db=db)

    # ── 工具 1：商品查询 ──
    agent.register_tool(AgentTool(
        name="get_product_info",
        description="查询商品的详细信息，包括名称、价格、库存、描述等",
        parameters={
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "商品 ID",
                },
            },
            "required": ["product_id"],
        },
        handler=lambda product_id: _get_product_info(
            product_id=product_id, db=db
        ),
    ))

    # ── 工具 2：订单查询 ──
    agent.register_tool(AgentTool(
        name="get_order_status",
        description="查询订单的当前状态，包括是否已支付、金额等",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单 ID（UUID 格式）",
                },
            },
            "required": ["order_id"],
        },
        handler=lambda order_id: _get_order_status(
            order_id=order_id,
            user_id=current_user_id,
            db=db,
        ),
    ))

    # ── 工具 3：秒杀活动列表 ──
    agent.register_tool(AgentTool(
        name="get_seckill_list",
        description="查询当前所有秒杀活动，包括活动时间、价格、库存等",
        parameters={
            "type": "object",
            "properties": {},  # 无参数
        },
        handler=lambda: _get_seckill_list(db=db),
    ))

    # ── 工具 4：秒杀下单 ──
    agent.register_tool(AgentTool(
        name="place_seckill_order",
        description="执行秒杀下单操作，必须先确认用户想参与哪个秒杀活动",
        parameters={
            "type": "object",
            "properties": {
                "seckill_product_id": {
                    "type": "integer",
                    "description": "秒杀活动 ID",
                },
            },
            "required": ["seckill_product_id"],
        },
        handler=lambda seckill_product_id: _place_seckill_order(
            seckill_product_id=seckill_product_id,
            user_id=current_user_id or "",
            redis=redis,
            db=db,
        ),
    ))

    logger.info(
        f"Agent created with tools: {[t.name for t in agent._tools.values()]}"
        f"{' for user: ' + current_user_id if current_user_id else ''}"
    )
    return agent