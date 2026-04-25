from typing import Generic, TypeVar, Optional
from pydantic import BaseModel, Field

T = TypeVar("T")

class APIResponse(BaseModel, Generic[T]):
    """Standard API response wrapper"""
    success: bool = Field(default=True, description="Whether the request succeeded")
    message: Optional[str] = Field(default=None, description="Optional message")
    data: Optional[T] = Field(default=None, description="Response data")

    class Config:
        from_attributes = True