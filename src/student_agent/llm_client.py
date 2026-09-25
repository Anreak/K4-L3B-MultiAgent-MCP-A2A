from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx2

logger = logging.getLogger(__name__)

CODE_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")


class SmallLLMClient:
    """Lightweight, resilient client for models <= 10B parameters.

    Supports:
    - Google Gemini (e.g. gemini-1.5-flash-8b) via GEMINI_API_KEY / GOOGLE_API_KEY
    - OpenAI-compatible (e.g. llama-3.1-8b-instant) via OPENAI_API_KEY / GROQ_API_KEY
    - Local endpoints via OLLAMA_HOST (e.g. http://localhost:11434/v1)

    Design Principles:
    1. Zero-downtime fallback: If no API key is provided or request fails, returns None.
    2. Strict parameter limit: Configured strictly for models <= 10B parameters.
    3. Strict timeout: Guarded by 5-second timeout to prevent stalling batch runs.
    """

    def __init__(
        self,
        model_name: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.timeout = timeout
        self.gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
        self.ollama_host = os.getenv("OLLAMA_HOST")

        # Determine provider and model
        if self.gemini_key:
            self.provider = "gemini"
            self.model_name = model_name or os.getenv("MODEL_NAME", "gemini-1.5-flash-8b")
        elif self.openai_key:
            self.provider = "openai"
            self.model_name = model_name or os.getenv("MODEL_NAME", "llama-3.1-8b-instant")
            self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").rstrip(
                "/"
            )
        elif self.ollama_host:
            self.provider = "ollama"
            self.model_name = model_name or os.getenv("MODEL_NAME", "qwen2.5:7b")
            self.base_url = f"{self.ollama_host.rstrip('/')}/v1"
        else:
            self.provider = "none"
            self.model_name = "none"

    def is_available(self) -> bool:
        """Check if an LLM provider is actively configured."""
        return self.provider != "none"

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        """Generate structured JSON from the model with strict exception handling and parsing."""
        if not self.is_available():
            return None

        try:
            if self.provider == "gemini":
                return await self._call_gemini(system_prompt, user_prompt)
            elif self.provider in ("openai", "ollama"):
                return await self._call_openai(system_prompt, user_prompt)
        except Exception as exc:
            logger.warning("Small LLM inference encountered error, falling back to rules: %s", exc)
            return None

        return None

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent"
            f"?key={self.gemini_key}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.0,
            },
        }

        async with httpx2.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning("Gemini API error status %s: %s", resp.status_code, resp.text)
                return None
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return self._parse_json(text)

    async def _call_openai(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.openai_key or 'ollama'}"}
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

        async with httpx2.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "OpenAI-compatible API error status %s: %s", resp.status_code, resp.text
                )
                return None
            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            text = choices[0].get("message", {}).get("content", "")
            return self._parse_json(text)

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        text = text.strip()
        match = CODE_BLOCK_PATTERN.search(text)
        if match:
            text = match.group(1).strip()
        try:
            val = json.loads(text)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass
        return None
