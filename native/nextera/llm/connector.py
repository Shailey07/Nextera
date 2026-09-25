import json
import time
import re
import httpx
from ..config import (GROQ_API_URL, GROQ_API_KEY, GROQ_MODEL,
                      GEMINI_API_KEY, GEMINI_MODEL)
from pydantic import BaseModel
from typing import Any, List, Optional
from loguru import logger


class ResponseFormat(BaseModel):
    question_id: str
    chosen: Optional[List[str]] = None
    answer: Optional[str] = None


class ResponseList(BaseModel):
    responses: List[ResponseFormat]


DEFAULT_RESPONSE_SCHEMA = ResponseList.model_json_schema()


def _parse_json_response(content: str) -> dict:
    """Robustly parse JSON from LLM response."""
    content = content.strip()

    if content.startswith("```"):
        parts = content.split("```")
        if len(parts) >= 2:
            content = parts[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Direct JSON parse failed, trying repair...")

    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    repaired = content.replace("'", '"').replace("True", "true").replace("False", "false")
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    logger.error(f"JSON parse failed. Content: {content[:500]}")
    raise ValueError("Could not parse LLM JSON response")


class GroqConnector(object):
    """Groq API — with Gemini fallback."""

    def __init__(self):
        if not GROQ_API_KEY:
            raise RuntimeError("No Groq API key.")
        self.api_key = GROQ_API_KEY
        self.model = GROQ_MODEL
        self.api_url = GROQ_API_URL

    def get_response(
            self,
            prompt: dict | str,
            system_prompt: str,
            response_schema: dict[str, Any] | None = None
    ) -> dict | str:
        logger.debug(f"Groq request ({self.model})...")

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

        max_retries = 2
        base_delay = 2
        last_error = None

        for attempt in range(max_retries):
            try:
                response = httpx.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=60.0  # 20s → 60s (bade batch ke liye)
                )

                logger.debug(f"Groq status: {response.status_code}")

                if response.status_code == 429:
                    logger.warning("Groq 429 rate limit")
                    raise Exception("429 rate limit")

                if response.status_code >= 500:
                    raise Exception(f"{response.status_code} server error")

                if response.status_code >= 400:
                    logger.error(f"Groq client error {response.status_code}: {response.text[:300]}")
                    raise Exception(f"{response.status_code} client error")

                data = response.json()

                if "choices" not in data or not data["choices"]:
                    logger.error(f"Groq returned no choices: {json.dumps(data)[:500]}")
                    raise Exception("No choices in response")

                content = data["choices"][0]["message"]["content"]
                logger.debug(f"Groq content length: {len(content)}")

                if response_schema is not None:
                    return _parse_json_response(content)
                return content.strip()

            except Exception as e:
                last_error = e
                error_str = str(e)
                logger.error(f"Groq attempt {attempt + 1}/{max_retries} failed: {error_str}")

                if attempt < max_retries - 1:
                    delay = base_delay * (attempt + 1)
                    logger.warning(f"Retrying in {delay}s...")
                    time.sleep(delay)
                    continue

        # Try Gemini fallback
        if GEMINI_API_KEY:
            logger.warning(f"Groq failed — trying Gemini fallback")
            try:
                return GeminiConnector().get_response(prompt, system_prompt, response_schema)
            except Exception as gemini_err:
                logger.error(f"Gemini fallback also failed: {gemini_err}")

        logger.error(f"All LLM providers failed: {last_error}")
        raise last_error or Exception("LLM failed")


class GeminiConnector(object):
    """Gemini API — fallback."""

    def __init__(self):
        if not GEMINI_API_KEY:
            raise RuntimeError("No Gemini API key.")
        from google import genai
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def get_response(
            self,
            prompt: dict | str,
            system_prompt: str,
            response_schema: dict[str, Any] | None = None
    ) -> dict | str:
        from google.genai import types

        logger.debug(f"Gemini request ({GEMINI_MODEL})...")
        config_args = {"system_instruction": system_prompt}
        if response_schema is not None:
            config_args["response_schema"] = response_schema
            config_args["response_mime_type"] = "application/json"

        config = types.GenerateContentConfig(**config_args)

        max_retries = 2
        base_delay = 2
        last_error = None

        for attempt in range(max_retries):
            try:
                contents = self._build_contents(prompt)
                response = self.client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=config
                )
                raw_text = response.candidates[0].content.parts[0].text
                logger.debug(f"Gemini response length: {len(raw_text)}")
                if response_schema is not None:
                    return _parse_json_response(raw_text)
                return raw_text.strip()
            except Exception as e:
                last_error = e
                logger.error(f"Gemini attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (attempt + 1))
                    continue
                raise

        raise last_error or Exception("Gemini failed")

    def _build_contents(self, prompt):
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, dict) and "images" in prompt:
            parts = []
            text_data = {k: v for k, v in prompt.items() if k != "images"}
            parts.append(json.dumps(text_data))
            for img in prompt["images"]:
                if isinstance(img, dict) and "url" in img:
                    parts.append({
                        "inline_data": {
                            "mime_type": img.get("mime_type", "image/png"),
                            "data": img["data"]
                        }
                    })
            return parts
        return json.dumps(prompt)


class PerplexityConnector(object):
    def get_response(self, prompt, system_prompt, response_schema=None):
        logger.warning("Perplexity deprecated — falling back.")
        if GEMINI_API_KEY:
            return GeminiConnector().get_response(prompt, system_prompt, response_schema)
        if GROQ_API_KEY:
            return GroqConnector().get_response(prompt, system_prompt, response_schema)
        raise RuntimeError("No LLM API key available.")