from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas.prompt import PromptEnhanceRequest, PromptEnhanceResponse
from app.schemas.common import APIResponse
from app.services.prompt_service import PromptService

router = APIRouter(prefix="/prompt", tags=["prompt enhancement"])

@router.post("/enhance", response_model=APIResponse[PromptEnhanceResponse])
async def enhance_prompt(
    request: PromptEnhanceRequest,
):
    """
    增强提示文本
    
    使用AI（如果已配置）或基于规则的方法增强给定的提示文本。
    根据指定的风格和语言优化提示的清晰度、上下文和表达方式。
    
    Args:
        request (PromptEnhanceRequest): 提示增强请求，包含：
            - prompt (str): 要增强的原始文本（必需）
            - style (str): 增强风格，可选值：
                - "professional": 专业风格，添加专业术语和框架
                - "creative": 创意风格，增加比喻和生动描述
                - "concise": 简洁风格，精简语言，突出重点
                - "detailed": 详细风格，扩展细节，提供示例
                - 默认值: "professional"
            - language (str): 目标语言代码，如 "zh"（中文）、"en"（英文）
                - 默认值: "en"
                
    Returns:
        APIResponse[PromptEnhanceResponse]: 增强后的提示响应
        
    Raises:
        HTTPException:
            - 400: 请求参数无效
            - 500: AI服务未配置或内部错误
            - 504: 请求超时（AI服务响应过慢）
            
    Example:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/prompt/enhance" \
          -H "Content-Type: application/json" \
          -d '{
            "prompt": "如何优化数据库查询？",
            "style": "professional",
            "language": "zh"
          }'
        ```
        
    Response Example (成功):
        ```json
        {
          "success": true,
          "message": "Prompt enhanced successfully",
          "data": {
            "original_prompt": "如何优化数据库查询？",
            "enhanced_prompt": "为了优化数据库查询性能，建议采取以下专业措施：1. 为频繁查询的字段创建索引...",
            "style": "professional",
            "language": "zh",
            "enhancement_method": "AI"
          }
        }
        ```
        
    Response Example (降级到规则增强):
        ```json
        {
          "success": true,
          "message": "Prompt enhanced successfully",
          "data": {
            "original_prompt": "如何优化数据库查询？",
            "enhanced_prompt": "优化数据库查询的方法包括：1. 使用索引 2. 避免SELECT * 3. 优化JOIN语句...",
            "style": "professional",
            "language": "zh",
            "enhancement_method": "rule_based"
          }
        }
        ```
        
    Note:
        - 如果配置了AI服务（OPENAI_API_KEY或SC_NET_API_KEY），将使用AI模型进行增强
        - 如果AI服务不可用，将自动降级到基于规则的增强方法
        - 请求超时时间为10秒，如果AI服务响应过慢可能返回超时错误
        - 支持中英文等多种语言
    """
    try:
        enhanced = await PromptService.enhance_prompt(request)
        return APIResponse(
            data=enhanced,
            message="Prompt enhanced successfully"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enhance prompt: {str(e)}"
        )