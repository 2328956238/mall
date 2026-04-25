from pydantic import BaseModel, Field
from typing import Optional

class PromptEnhanceRequest(BaseModel):
    """Request model for prompt enhancement"""
    prompt: str = Field(..., min_length=1, max_length=2000, description="The original prompt to enhance")
    style: Optional[str] = Field(
        default="professional",
        description="Enhancement style: professional, creative, concise, detailed"
    )
    language: Optional[str] = Field(default="en", description="Target language code")

class PromptEnhanceResponse(BaseModel):
    """Response model for enhanced prompt"""
    original_prompt: str
    enhanced_prompt: str
    style: str
    language: str
    improvements: list[str] = Field(
        default_factory=list,
        description="List of improvements made (e.g., 'added context', 'rephrased')"
    )