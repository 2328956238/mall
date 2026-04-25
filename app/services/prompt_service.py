import logging
import httpx
from typing import Optional
from app.config import settings
from app.schemas.prompt import PromptEnhanceRequest, PromptEnhanceResponse

logger = logging.getLogger(__name__)

class PromptService:
    """Service for enhancing prompts using AI or rule-based methods"""

    @staticmethod
    async def enhance_prompt(request: PromptEnhanceRequest) -> PromptEnhanceResponse:
        """
        Enhance a prompt based on style and language.
        If SC_NET_API_KEY is set, uses SC Net API; otherwise uses rule-based enhancement.
        """
        # Check if SC Net API is available
        if settings.SC_NET_API_KEY and settings.SC_NET_API_KEY.strip():
            try:
                return await PromptService._enhance_with_scnet(request)
            except Exception as e:
                logger.warning(f"SC Net API enhancement failed: {e}, falling back to rule-based")

        # Fallback to rule-based enhancement
        return PromptService._enhance_rule_based(request)

    @staticmethod
    async def _enhance_with_scnet(request: PromptEnhanceRequest) -> PromptEnhanceResponse:
        """Enhance prompt using SC Net API (compatible with OpenAI API)"""
        style_instructions = {
            "professional": "Make the prompt more professional and business-appropriate.",
            "creative": "Make the prompt more creative and imaginative.",
            "concise": "Make the prompt more concise and to the point.",
            "detailed": "Add more detail and specificity to the prompt.",
        }.get(request.style, "Improve the prompt.")

        system_message = f"You are a helpful assistant that improves user prompts. {style_instructions} Keep the meaning the same. Respond only with the enhanced prompt."

        headers = {
            "Authorization": f"Bearer {settings.SC_NET_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": settings.SC_NET_MODEL,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 500,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{settings.SC_NET_API_BASE}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                
                if "choices" not in data or len(data["choices"]) == 0:
                    raise RuntimeError("Invalid response format from SC Net API")
                
                enhanced = data["choices"][0]["message"]["content"].strip()
        except httpx.HTTPError as e:
            logger.error(f"SC Net API HTTP error: {e}, response: {e.response.text if e.response else 'No response'}")
            raise RuntimeError(f"SC Net API HTTP error: {e}")
        except KeyError as e:
            logger.error(f"SC Net API response format error: {e}, data: {data}")
            raise RuntimeError(f"SC Net API response format error: {e}")
        except Exception as e:
            logger.error(f"SC Net API error: {e}", exc_info=True)
            raise RuntimeError(f"SC Net API error: {e}")

        improvements = ["AI-enhanced (SC Net)", f"style: {request.style}"]
        return PromptEnhanceResponse(
            original_prompt=request.prompt,
            enhanced_prompt=enhanced,
            style=request.style,
            language=request.language,
            improvements=improvements,
        )

    @staticmethod
    def _enhance_rule_based(request: PromptEnhanceRequest) -> PromptEnhanceResponse:
        """Rule-based prompt enhancement (fallback)"""
        prompt = request.prompt
        improvements = []

        # Add context based on style
        if request.style == "professional":
            enhanced = f"Please consider the following professional request: {prompt}"
            improvements.append("Added professional framing")
        elif request.style == "creative":
            enhanced = f"Imagine and elaborate on: {prompt}"
            improvements.append("Added creative framing")
        elif request.style == "concise":
            # Simple attempt to make concise (just a placeholder)
            enhanced = prompt[:100] + "..." if len(prompt) > 100 else prompt
            improvements.append("Attempted conciseness")
        elif request.style == "detailed":
            enhanced = f"Provide detailed information about: {prompt}"
            improvements.append("Added detail request")
        else:
            enhanced = f"Enhanced: {prompt}"
            improvements.append("Basic enhancement")

        # Language note
        if request.language != "en":
            improvements.append(f"Target language: {request.language}")

        return PromptEnhanceResponse(
            original_prompt=request.prompt,
            enhanced_prompt=enhanced,
            style=request.style,
            language=request.language,
            improvements=improvements,
        )