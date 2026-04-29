"""
对话级上下文构建器
=================
为 Agent 的 Layer 2 任务模板注入实时上下文。

解决的问题：
  之前 reason() 只做了意图检测 + 模板渲染，但没有注入用户级和系统级上下文。
  比如用户说"帮我看看我的订单"，Agent 不知道用户的 ID、历史订单状态、当前活跃秒杀。

上下文来源（三类）：
  1. 用户上下文：用户基本信息 + 最近 N 条订单
  2. 系统上下文：当前活跃秒杀活动数量、热门商品
  3. 对话上下文：历史意图、已调用的工具列表

使用方式：
    context = await ContextBuilder(redis, db, user_id).build(user_message, history)
    system_prompt = loader.build_full_prompt(
        user_message=user_message,
        tool_descriptions=tool_descriptions,
        custom_context=context,  # ← 注入实时上下文
    )
"""

import logging
from typing import Optional, Any
from datetime import datetime, timezone
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.user import User
from app.models.order import Order, OrderStatus
from app.models.seckill_product import SeckillProduct
from app.models.product import Product

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    上下文构建器 - 收集用户/系统/对话三类上下文。

    设计原则：
      - 轻量：只查询必要数据，避免大量 DB 查询拖慢 Agent 响应
      - 安全：不泄露密码、token 等敏感信息
      - 可缓存：系统级上下文可以缓存（如活跃秒杀数量）
    """

    # 用户最近订单数
    MAX_RECENT_ORDERS = 3
    # 秒杀活动缓存 TTL（秒）
    SECKILL_CACHE_TTL = 30

    def __init__(
        self,
        redis: Redis,
        db: AsyncSession,
        user_id: Optional[str] = None,
    ):
        self.redis = redis
        self.db = db
        self.user_id = user_id

    async def build(
        self,
        user_message: str,
        history: Optional[list[dict]] = None,
    ) -> str:
        """
        构建完整的上下文文本块。

        Args:
            user_message: 用户当前消息
            history: 历史对话消息

        Returns:
            格式化的上下文文本（Markdown），直接拼接到 system prompt 中
        """
        sections = []

        # 1. 用户上下文
        user_context = await self._build_user_context()
        if user_context:
            sections.append(user_context)

        # 2. 系统上下文
        system_context = await self._build_system_context()
        if system_context:
            sections.append(system_context)

        # 3. 对话上下文
        dialog_context = self._build_dialog_context(user_message, history)
        if dialog_context:
            sections.append(dialog_context)

        if not sections:
            return ""

        return "\n\n".join(sections)

    # ──────────────────────────────────────────────
    # 1. 用户上下文
    # ──────────────────────────────────────────────

    async def _build_user_context(self) -> Optional[str]:
        """
        构建用户上下文。

        包含：
          - 用户基本信息（名称、注册时间）
          - 最近 N 条订单（状态、金额、时间）

        Returns:
            Markdown 格式的用户上下文文本
        """
        if not self.user_id:
            return None

        import uuid
        try:
            user_uuid = uuid.UUID(self.user_id)
        except ValueError:
            return None

        # 查询用户
        result = await self.db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if not user:
            return None

        lines = [
            "## 当前用户信息",
            f"- 用户昵称：{user.full_name}",
            f"- 注册邮箱：{user.email}",
            f"- 用户ID：{self.user_id[:8]}...{self.user_id[-4:]}",
        ]

        # 查询最近订单
        order_result = await self.db.execute(
            select(Order)
            .where(Order.user_id == user_uuid)
            .order_by(Order.created_at.desc())
            .limit(self.MAX_RECENT_ORDERS)
        )
        orders = order_result.scalars().all()

        if orders:
            lines.append("")
            lines.append(f"### 最近 {len(orders)} 条订单")
            for o in orders:
                status_label = {
                    OrderStatus.PENDING: "⏳ 待支付",
                    OrderStatus.PAID: "✅ 已支付",
                    OrderStatus.SHIPPED: "📦 已发货",
                    OrderStatus.COMPLETED: "🎉 已完成",
                    OrderStatus.CANCELLED: "❌ 已取消",
                }.get(o.status, str(o.status.value))

                created = o.created_at.strftime("%m-%d %H:%M") if o.created_at else "未知"
                lines.append(
                    f"  - 订单 `{str(o.id)[:8]}...` | "
                    f"￥{o.total_amount:.2f} | "
                    f"{status_label} | "
                    f"{created}"
                )

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 2. 系统上下文
    # ──────────────────────────────────────────────

    async def _build_system_context(self) -> Optional[str]:
        """
        构建系统上下文。

        包含：
          - 当前活跃秒杀活动数量
          - 当前时间（用于判断秒杀是否开始/结束）

        这些数据相对静态，可以加 Redis 缓存减少 DB 压力。
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # 查询活跃秒杀活动数量（进行中 + 未开始）
        cache_key = "agent:context:active_seckills"

        # 尝试从缓存读取
        cached = await self.redis.get(cache_key)
        if cached:
            try:
                active_count, upcoming_count = cached.decode().split(",")
                active_count = int(active_count)
                upcoming_count = int(upcoming_count)
            except (ValueError, AttributeError):
                cached = None

        if not cached:
            # 查询进行中的秒杀
            active_result = await self.db.execute(
                select(func.count(SeckillProduct.id))
                .where(SeckillProduct.start_time <= now)
                .where(SeckillProduct.end_time >= now)
            )
            active_count = active_result.scalar() or 0

            # 查询即将开始的秒杀
            upcoming_result = await self.db.execute(
                select(func.count(SeckillProduct.id))
                .where(SeckillProduct.start_time > now)
            )
            upcoming_count = upcoming_result.scalar() or 0

            # 写入缓存（30s TTL，因为秒杀活动状态变化较慢）
            await self.redis.setex(
                cache_key,
                self.SECKILL_CACHE_TTL,
                f"{active_count},{upcoming_count}",
            )

        lines = [
            "## 系统当前状态",
            f"- 当前时间（UTC）：{now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 进行中的秒杀活动：{active_count} 个",
            f"- 即将开始的秒杀活动：{upcoming_count} 个",
        ]

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 3. 对话上下文
    # ──────────────────────────────────────────────

    def _build_dialog_context(
        self,
        user_message: str,
        history: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """
        构建对话上下文。

        包含：
          - 当前轮次（第几轮对话）
          - 历史对话主题简述

        Args:
            user_message: 用户当前消息
            history: 历史消息列表

        Returns:
            Markdown 格式的对话上下文文本
        """
        if not history:
            return None

        # 计算有效轮次（user ↔ assistant 对）
        user_turns = sum(1 for m in history if m.get("role") == "user")
        if user_turns == 0:
            return None

        # 提取历史主题关键词（取最后几条用户消息的前 20 字）
        last_user_msgs = [
            m["content"][:20] + ("..." if len(m["content"]) > 20 else "")
            for m in reversed(history)
            if m.get("role") == "user"
        ][:3]  # 最多 3 条

        lines = [
            "## 对话上下文",
            f"- 这是第 {user_turns + 1} 轮对话",
            "- 用户之前提到过：",
        ]
        for msg in reversed(last_user_msgs):
            lines.append(f"  - 「{msg}」")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 内置入口（用于 factory）
    # ──────────────────────────────────────────────

    @staticmethod
    async def build_context(
        redis: Redis,
        db: AsyncSession,
        user_id: Optional[str] = None,
        user_message: str = "",
        history: Optional[list[dict]] = None,
    ) -> str:
        """
        快捷静态方法，一步构建完整上下文。

        这是给 factory 和路由层调用的统一入口。

        Args:
            redis: Redis 客户端
            db: 数据库会话
            user_id: 用户 ID
            user_message: 用户消息
            history: 历史消息

        Returns:
            上下文文本
        """
        builder = ContextBuilder(redis=redis, db=db, user_id=user_id)
        return await builder.build(user_message=user_message, history=history)