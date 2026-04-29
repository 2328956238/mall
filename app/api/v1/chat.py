from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.common import APIResponse
from app.services.chat_service import ChatService
from app.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/chat", tags=["智能客服"])


@router.post("/message", response_model=APIResponse[ChatResponse])
async def chat_message(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """
    智能客服对话接口

    与AI客服进行对话，支持上下文记忆和商品关联。

    Args:
        request (ChatRequest): 对话请求
            - message (str): 用户消息内容（必填）
            - history (list, optional): 历史对话记录，用于保持上下文
            - product_id (int, optional): 关联的商品ID，客服会提供针对性帮助

    Returns:
        APIResponse[ChatResponse]: 客服回复内容

    Examples:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/chat/message" \
            -H "Authorization: Bearer <token>" \
            -H "Content-Type: application/json" \
            -d '{
                "message": "最近有什么秒杀活动？",
                "history": []
            }'
        ```

    Note:
        - 需要登录认证（Bearer Token）
        - 如果配置了 SC Net API，将使用 AI 模型回复
        - 否则使用规则匹配的预设回复
        - 建议前端保留历史记录以便上下文对话
    """
    try:
        service = ChatService()
        result = await service.chat(request)
        return APIResponse(
            data=result,
            message="success",
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"对话服务异常: {str(e)}",
        )