import os
import requests
from src.config import PREFERRED_MODELS
from src.logging_config import setup_logging
from src.secrets import get_secret
import re

logger = setup_logging(__name__)

def _sanitize(text: str) -> str:
    """Detect and redact API keys/tokens from log strings."""
    SENSITIVE_PATTERNS = [
        re.compile(r'([?&](?:key|api_key|apikey|token|access_token)=)[^&\s"\']+', re.IGNORECASE),
        re.compile(r'(Bearer\s+)[^\s"\']+', re.IGNORECASE),
    ]

    if not isinstance(text, str):
        text = str(text)
        
    present = any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)
    if present:
        logger.warning("[SECURITY ALERT] Potential API key leak detected in error output! Redacting credentials before logging.")
        sanitized_text = text
        for pattern in SENSITIVE_PATTERNS:
            sanitized_text = pattern.sub(r'\1[REDACTED]', sanitized_text)
        return sanitized_text
        
    return text

# Module-level cache: {"groq": "openai/gpt-oss-20b", ...}
_resolved_models: dict[str, str] = {}

def _fetch_available_models(provider: str) -> list[str]:
    """Fetch the list of currently active model IDs from a provider's API.
    
    Args:
        provider: The provider name ("groq", "gemini", etc.).
        
    Returns:
        list[str]: List of active model ID strings.
        
    Raises:
        ValueError: If the provider is not supported.
        requests.RequestException: If the API call fails.
    """
    if provider == "groq":
        api_key = get_secret("GROQ_API_KEY")
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers = {"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        resp.raise_for_status()
        return [m["id"] for m in resp.json()["data"]]
    if provider == "gemini":
        api_key = get_secret("GOOGLE_API_KEY")
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=5,
        )
        resp.raise_for_status()
        # Gemini returns names like "models/gemini-2.5-flash", strip the prefix
        return [m["name"].removeprefix("models/") for m in resp.json()["models"]]
    else:
        raise ValueError(f"Auto-update not supported for provider: {provider}")

def _probe_model(provider: str, model_id: str) -> bool:
    """Make a minimal API call to verify a model is actually callable.
    
    Sends a 1-token request ("hi") to check the model responds.
    Returns True if the model works, False on any error (404, permission, timeout).
    
    Args:
        provider: The provider name ("groq", "gemini").
        model_id: The model ID string to probe.
        
    Returns:
        bool: True if the model responded successfully, False otherwise.
    """
    try:
        if provider == "groq":
            api_key = get_secret("GROQ_API_KEY")
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return True
        elif provider == "gemini":
            api_key = get_secret("GOOGLE_API_KEY")
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": "hi"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
                timeout=10,
            )
            resp.raise_for_status()
            return True
    except requests.RequestException as e:
        logger.warning(f"[ModelResolver] Probe failed for '{model_id}': {_sanitize(str(e))}")
        return False
    return False

def get_model(provider: str) -> str:
    """Return the best available model for a provider (cached after first call).
    
    Resolution order:
    1. Return from cache if already resolved.
    2. Fetch active models from the provider API.
    3. Build candidate list: preferred models (in order) that appear in active,
       then remaining active models.
    4. Probe each candidate with a minimal API call.
    5. First candidate that responds → cache and return.
    6. If all candidates fail probing → raise ValueError.
    
    Args:
        provider: The provider name ("groq", "gemini", etc.).
        
    Returns:
        str: The selected model ID string.
    """
    if provider in _resolved_models:
        return _resolved_models[provider]

    if provider == "claude_local":  # the CLI takes an alias (haiku, sonnet, opus): nothing to list or probe
        return PREFERRED_MODELS["claude_local"][0]

    preferred = PREFERRED_MODELS.get(provider, [])

    try:
        active = _fetch_available_models(provider)
        logger.debug(f"[ModelResolver] {provider} has {len(active)} active models:")
        for m in active:
            logger.debug(f"  - {m}")
    except Exception as e:
        logger.error(f"[ModelResolver] Failed to fetch {provider} models: {_sanitize(str(e))}")
        raise

    candidates = [m for m in preferred if m in active]
    candidates += [m for m in active if m not in candidates]

    # Probe each candidate until one works
    for model in candidates:
        logger.info(f"[ModelResolver] Probing '{model}' for {provider}...")
        if _probe_model(provider, model):
            _resolved_models[provider] = model
            logger.info(f"[ModelResolver] ✓ Selected '{model}' for {provider}")
            return model
        logger.warning(f"[ModelResolver] ✗ '{model}' not callable, trying next...")

    raise ValueError(
        f"[ModelResolver] All candidates failed for provider '{provider}'. "
        f"Tried: {candidates[:10]}..."
    )
