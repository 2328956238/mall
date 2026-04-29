import logging
import httpx
from typing import List, Optional
from datetime import datetime
from app.config import settings
from app.schemas.chat import ChatRequest, ChatResponse, ChatMessage

logger = logging.getLogger(__name__)

# 系统提示词 - 定义客服角色和知识范围
SYSTEM_PROMPT = """你是一个专业的电商平台智能客服助手，名叫"小闪"。你所在的平台是"闪电秒杀平台"（FlashSalesPlatform）。

## 你的能力范围
1. **商品咨询**：回答关于商品信息、价格、库存等问题
2. **秒杀活动**：解释秒杀规则、活动时间、抢购流程
3. **订单问题**：查询订单状态、支付方式、物流信息
4. **账户问题**：登录注册、密码重置、个人信息修改
5. **平台介绍**：介绍平台功能、技术特点、使用帮助

## 平台功能简介
- 高性能分布式秒杀系统，支持高并发抢购
- 支持支付宝和微信支付
- 提供实时订单通知（WebSocket）
- 管理员可管理商品、秒杀活动和订单

## 回复规则
- 用友好、热情的语气回复，语言简洁明了
- 如果用户询问超出知识范围的问题，礼貌地表示无法回答并引导到相关话题
- 使用中文回复
- 回复要结构化，适当使用emoji增加亲和力
- 如果用户需要具体操作指导，给出清晰的步骤说明

请开始对话！"""


class ChatService:
    """智能客服服务 - 基于AI的对话系统"""

    def __init__(self):
        self.api_key = settings.SC_NET_API_KEY
        self.api_base = settings.SC_NET_API_BASE
        self.model = settings.SC_NET_MODEL

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """
        处理用户对话请求
        如果配置了AI API则使用AI回复，否则返回预设回复
        """
        if self.api_key and self.api_key.strip():
            try:
                return await self._chat_with_ai(request)
            except Exception as e:
                logger.warning(f"AI chat failed: {e}, using fallback response")
                return self._fallback_response(request.message)

        # 无AI配置时的降级回复
        return self._fallback_response(request.message)

    async def _chat_with_ai(self, request: ChatRequest) -> ChatResponse:
        """使用AI进行对话"""
        # 构建消息列表
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        # 添加上下文信息（如果有商品ID）
        context_notes = ""
        if request.product_id:
            context_notes = f"\n用户正在咨询商品ID为 {request.product_id} 的商品信息。"
            messages.append({
                "role": "system",
                "content": f"注意：用户当前正在查看商品 #{request.product_id}，可以针对该商品提供具体帮助。"
            })

        # 添加历史消息
        for msg in request.history:
            if msg.role in ("user", "assistant"):
                messages.append({"role": msg.role, "content": msg.content})

        # 添加当前用户消息
        messages.append({"role": "user", "content": f"{context_notes}{request.message}"})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 0.9,
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

                if "choices" not in data or len(data["choices"]) == 0:
                    raise RuntimeError("Invalid response format from AI API")

                reply = data["choices"][0]["message"]["content"].strip()

                # 检查是否有引用来源
                sources = None
                if "citations" in data:
                    sources = data["citations"]

                return ChatResponse(
                    reply=reply,
                    timestamp=datetime.utcnow(),
                    sources=sources,
                )

        except httpx.TimeoutException:
            logger.error("AI API request timed out")
            return self._fallback_response(request.message, "（AI响应超时，请稍后重试）")
        except httpx.HTTPStatusError as e:
            logger.error(f"AI API HTTP error: {e}, response: {e.response.text if e.response else 'No response'}")
            raise RuntimeError(f"AI服务暂时不可用，请稍后再试")
        except Exception as e:
            logger.error(f"AI chat error: {e}", exc_info=True)
            raise RuntimeError(f"对话服务异常: {str(e)}")

    def _fallback_response(self, user_message: str, extra_note: str = "") -> ChatResponse:
        """无AI配置时的降级回复"""
        user_message_lower = user_message.lower()
        reply = f"您好！我是小闪，闪电秒杀平台的智能客服。{extra_note}\n\n"

        # 简单的关键词匹配回复
        if any(kw in user_message for kw in ["你好", "您好", "hi", "hello", "嗨"]):
            reply += "欢迎来到闪电秒杀平台！⚡\n\n我可以帮您：\n"
            reply += "1️⃣ 查询商品信息和秒杀活动\n"
            reply += "2️⃣ 解答订单和支付问题\n"
            reply += "3️⃣ 提供平台使用帮助\n\n"
            reply += "请问有什么可以帮您的吗？😊"
        elif any(kw in user_message for kw in ["秒杀", "抢购", "seckill"]):
            reply += "关于秒杀活动：\n\n"
            reply += "⚡ **秒杀规则**：\n"
            reply += "  - 秒杀活动在指定时间开始，先到先得\n"
            reply += "  - 每个用户每场秒杀限购1件\n"
            reply += "  - 秒杀成功后请在15分钟内完成支付\n\n"
            reply += "💡 **小贴士**：\n"
            reply += "  - 建议提前登录账号\n"
            reply += "  - 提前进入秒杀页面等待\n"
            reply += "  - 网络环境要稳定哦！\n\n"
            reply += "还有什么想了解的吗？😊"
        elif any(kw in user_message for kw in ["订单", "order", "支付", "付款"]):
            reply += "关于订单和支付：\n\n"
            reply += "📋 **订单查询**：在「我的订单」页面可以查看所有订单\n"
            reply += "💳 **支付方式**：支持支付宝和微信支付\n"
            reply += "⏰ **支付时限**：订单生成后15分钟内完成支付\n\n"
            reply += "需要我帮您查询具体订单吗？"
        elif any(kw in user_message for kw in ["商品", "product", "价格", "库存"]):
            reply += "关于商品信息：\n\n"
            reply += "📦 您可以在「商品」页面浏览所有商品\n"
            reply += "💲 商品价格和库存信息都会实时更新\n"
            reply += "⚡ 参与秒杀的商品会有额外优惠价格\n\n"
            reply += "需要我帮您查找特定商品吗？"
        else:
            reply += "我是小闪，可以帮您解答关于商品、秒杀、订单等方面的问题。\n\n"
            reply += "您可以直接问我，例如：\n"
            reply += "- \"最近有什么秒杀活动？\"\n"
            reply += "- \"如何查看我的订单？\"\n"
            reply += "- \"支付支持哪些方式？\"\n\n"
            reply += "请告诉我您需要什么帮助！😊"

        return ChatResponse(
            reply=reply,
            timestamp=datetime.utcnow(),
            sources=["预设回复（AI服务未配置）"],
        )