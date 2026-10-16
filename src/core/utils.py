import math
import os
from src.config import GROQ_MAX_RETRIES
from src.logging_config import setup_logging
from src.routing.model_resolver import get_model
from src.secrets import get_secret

logger = setup_logging(__name__)

# Seconds to wait after a rate limit whose response names no wait
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 10


class EmptyAnswerError(Exception):
    """The LLM answered with blank text twice in a row."""


def create_llm(provider: str):
    """Factory function to create LLM instances based on the provider name.
    
    Args:
        provider: The name of the LLM provider: 'groq', 'gemini' or 'claude_local' (the claude CLI on this
            machine; local runs only, see src/core/claude_cli.py).

    Returns:
        The appropriate LLM instance for the specified provider.

    Raises:
        ValueError: If an unknown provider is specified, or claude_local is asked for outside a developer machine.
    """
    if provider == "claude_local":
        from src.core.claude_cli import ChatClaudeCLI, blocked_reason
        reason = blocked_reason()
        if reason:
            raise ValueError(f"claude_local is for local runs only, and {reason}. "
                             f"Use groq or gemini here (MAIN_LLM_MODEL, EVAL_LLM_MODEL).")
        return ChatClaudeCLI(model_name=get_model(provider))
    model = get_model(provider)

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model = model,
            temperature=0,
            google_api_key=get_secret("GOOGLE_API_KEY")
        )
    elif provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            model = model,
            temperature=0,
            max_retries=GROQ_MAX_RETRIES,
            groq_api_key=get_secret("GROQ_API_KEY")
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")


def invoke_text(llm, prompt: str) -> str:
    """Call the LLM and return its text, asking once more when the text is blank.

    gpt-oss-20b on Groq has answered 200 with empty content (2026-10-07). The warning logs the finish reason
    and token usage, to find out why.

    Raises:
        EmptyAnswerError: when the second answer is blank too.
    """
    for attempt in (1, 2):
        response = llm.invoke(prompt)
        if response.content.strip():
            return response.content
        finish_reason = getattr(response, "response_metadata", {}).get("finish_reason")
        logger.warning(f"[LLM] Empty answer (attempt {attempt}/2, finish reason {finish_reason}, "
                       f"usage {getattr(response, 'usage_metadata', None)})")
    raise EmptyAnswerError("The LLM returned an empty answer twice")


def rate_limit_retry_after(error: Exception) -> int | None:
    """Seconds to wait when error is the LLM provider's rate limit (HTTP 429), or None for any other error.

    Such an error reaches the caller only after the SDK's own retries (GROQ_MAX_RETRIES). The wait is the response's
    retry-after header rounded up, or DEFAULT_RATE_LIMIT_WAIT_SECONDS when the header is missing or unreadable.
    """
    if getattr(error, "status_code", None) != 429:
        return None
    try:
        return max(1, math.ceil(float(error.response.headers["retry-after"])))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS
