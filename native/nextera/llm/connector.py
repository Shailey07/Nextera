import json
import time
import httpx
from ..config import (GROQ_API_URL, GROQ_API_KEY, GROQ_MODEL,
                      GEMINI_API_KEY, GEMINI_MODEL)
from pydantic import BaseModel
from typing import Any, List, Literal, Optional
from loguru import logger


class ResponseFormat(BaseModel):
    question_id: str
    question_type: Literal["MULTIPLE_CHOICE", "CHECKBOX", "TEXT_REFLECT"]
    chosen: Optional[List[str]] = None
    answer: Optional[str] = None


class ResponseList(BaseModel):
    responses: List[ResponseFormat]


DEFAULT_RESPONSE_SCHEMA = ResponseList.model_json_schema()


class GroqConnector(object):
    """Groq API — OpenAI-compatible, 14,400 requests/day free."""

    def __init__(self):
        if not GROQ_API_KEY:
            raise RuntimeError("No Groq API key. Add groq_api_key to ~/.nextera/config.json")
        self.api_key = GROQ_API_KEY
        self.model = GROQ_MODEL
        self.api_url = GROQ_API_URL

    def get_response(
            self,
            prompt: dict | str,
            system_prompt: str,
            response_schema: dict[str, Any] | None = None
    ) -> dict | str:
        logger.debug(f"Making an API request to Groq ({self.model})...")

        user_content = json.dumps(prompt) if isinstance(prompt, dict) else prompt

        if response_schema is not None:
            schema_str = json.dumps(response_schema, indent=2)
            system_prompt += (
                f"\n\nYou MUST respond with valid JSON only. "
                f"No markdown, no explanations, no code fences. "
                f"Your entire response must be a single JSON object "
                f"matching this exact schema:\n\n{schema_str}"
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        }

        if response_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        max_retries = 5
        base_delay = 10

        for attempt in range(max_retries):
            try:
                response = httpx.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=120.0
                )

                if response.status_code == 429:
                    raise Exception("429 rate limit exceeded")
                if response.status_code >= 500:
                    raise Exception(f"{response.status_code} server error")

                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]

                if response_schema is not None:
                    content = content.strip()
                    if content.startswith("```"):
                        content = content.split("```")[1]
                        if content.startswith("json"):
                            content = content[4:]
                        content = content.strip()
                    return json.loads(content)
                return content.strip()

            except Exception as e:
                error_str = str(e)
                is_retryable = (
                    "429" in error_str or "rate limit" in error_str.lower()
                    or "503" in error_str or "500" in error_str
                    or "timeout" in error_str.lower()
                )
                if is_retryable and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Groq API busy/rate-limited. Retry {attempt + 1}/{max_retries} in {delay}s...")
                    time.sleep(delay)
                    continue
                logger.error(f"Groq error: {error_str}")
                raise


class GeminiConnector(object):
    def __init__(self):
        if not GEMINI_API_KEY:
            raise RuntimeError("No Gemini API key. Add gemini_api_key to ~/.nextera/config.json")
        from google import genai
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def get_response(
            self,
            prompt: dict | str,
            system_prompt: str,
            response_schema: dict[str, Any] | None = None
    ) -> dict | str:
        from google.genai import types

        logger.debug(f"Making an API request to Gemini ({GEMINI_MODEL})...")
        config_args = {"system_instruction": system_prompt}
        if response_schema is not None:
            config_args["response_schema"] = response_schema
            config_args["response_mime_type"] = "application/json"

        config = types.GenerateContentConfig(**config_args)

        max_retries = 5
        base_delay = 3

        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=json.dumps(prompt) if isinstance(prompt, dict) else prompt,
                    config=config
                )
                raw_text = response.candidates[0].content.parts[0].text
                if response_schema is not None:
                    return json.loads(raw_text)
                return raw_text.strip()
            except Exception as e:
                error_str = str(e)
                is_retryable = (
                    "503" in error_str or "UNAVAILABLE" in error_str
                    or "429" in error_str or "high demand" in error_str
                )
                if is_retryable and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Gemini API busy. Retry {attempt + 1}/{max_retries} in {delay}s...")
                    time.sleep(delay)
                    continue
                raise


class PerplexityConnector(object):
    """Deprecated — falls back to Groq/Gemini."""

    def get_response(
            self,
            prompt: dict | str,
            system_prompt: str,
            response_schema: dict[str, Any] | None = None
    ) -> dict | str:
        logger.warning("Perplexity deprecated — falling back.")
        if GROQ_API_KEY:
            return GroqConnector().get_response(prompt, system_prompt, response_schema)
        if GEMINI_API_KEY:
            return GeminiConnector().get_response(prompt, system_prompt, response_schema)
        raise RuntimeError("No LLM API key available.")