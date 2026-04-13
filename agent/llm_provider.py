"""
Centralised LLM provider factory.

Set LLM_PROVIDER in .env to choose the backend:
  • "google"  → Google Gemini  (via langchain-google-genai)
  • "groq"    → Groq           (via langchain-openai, OpenAI-compatible)
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Supported providers ─────────────────────────────────────────────
SUPPORTED_PROVIDERS = ("google", "groq")


def get_llm(provider: str | None = None, model: str | None = None):
    """Return a LangChain ChatModel for the configured provider.

    Args:
        provider: Override the LLM_PROVIDER env var (e.g. "google", "groq").
        model:    Override the MODEL / GROQ_MODEL env var.
    """
    provider = (provider or os.getenv("LLM_PROVIDER", "google")).lower().strip()

    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unknown LLM_PROVIDER='{provider}'. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )

    # ── Google Gemini ────────────────────────────────────────────────
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GOOGLE_API_KEY is required when LLM_PROVIDER=google"
            )

        model = model or os.getenv("MODEL", "gemini-2.5-flash-lite")
        print(f"    🤖 Using Google model: {model}")
        logger.info("LLM init provider=google model=%s", model)
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=0,
        )

    # ── Groq (OpenAI-compatible) ─────────────────────────────────────
    if provider == "groq":
        from langchain_openai import ChatOpenAI

        # Accept both spellings the user might have in .env
        api_key = os.getenv("GROQ_API_KEY") or os.getenv("GROK_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY is required when LLM_PROVIDER=groq"
            )

        model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        print(f"    🤖 Using Groq model: {model}")
        logger.info("LLM init provider=groq model=%s", model)
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
            temperature=0,
            max_retries=0,
            timeout=30.0,
        )
