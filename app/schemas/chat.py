from pydantic import BaseModel, Field
from typing import List, Optional, Any
from datetime import datetime


class ChatMessage(BaseModel):
    """单个聊天消息"""
    role: str = Field(..., description="角色: user 或 assistant")
    content: str = Field(..., description="消息内容")
    timestamp: Optional[datetime] = Field(default=None, description="消息时间")


class ChatRequest(BaseModel):
    """客服对话请求"""
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    history: Optional[List[ChatMessage]] = Field(default_factory=list, description="历史对话记录")
    product_id: Optional[int] = Field(default=None, description="相关商品ID（可选）")


class ChatResponse(BaseModel):
    """客服对话响应"""
    reply: str = Field(..., description="AI回复内容")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="响应时间")
    sources: Optional[List[str]] = Field(default=None, description="引用来源（如有）")


class AgentThoughtSchema(BaseModel):
    """Agent 推理步骤 - 用于前端展示和链路追踪"""
    step: int = Field(..., description="推理步骤序号")
    thought: str = Field(default="", description="推理思考过程")
    action: Optional[dict[str, Any]] = Field(default=None, description="调用的工具和参数")
    observation: Optional[str] = Field(default=None, description="工具返回结果")
    tool_latency_ms: Optional[float] = Field(default=None, description="工具调用耗时")


class AgentChatRequest(BaseModel):
    """Agent 对话请求"""
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    history: Optional[List[ChatMessage]] = Field(default_factory=list, description="历史对话记录")


class AgentChatResponse(BaseModel):
    """Agent 对话响应，包含推理轨迹"""
    reply: str = Field(..., description="Agent 最终回复")
    thoughts: List[AgentThoughtSchema] = Field(default_factory=list, description="推理步骤链（可用于前端展示推理过程）")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="响应时间")