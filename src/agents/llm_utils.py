import time


class TransientLLMError(Exception):
    """Retryable: rate limit (429), a server error (5xx), or a
    network/connection failure. Caught and retried by call_llm_with_retry."""


class LLMUnavailableError(Exception):
    """Raised once retries are exhausted. Callers decide what "clean
    failure" means for them (the buyer ends the negotiation; the merchant
    falls back to rules-only for that round)."""


def call_llm_with_retry(llm_call, system, user_content, model, max_retries=3, base_delay=1.0, max_delay=20.0, sleep_fn=time.sleep):
    """Retries on TransientLLMError with exponential backoff. After
    max_retries, raises LLMUnavailableError instead of propagating the raw
    transient error."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return llm_call(system, user_content, model)
        except TransientLLMError as exc:
            last_exc = exc
            if attempt < max_retries:
                sleep_fn(min(base_delay * (2 ** attempt), max_delay))
    raise LLMUnavailableError(
        f"LLM backend unavailable after {max_retries + 1} attempts: {last_exc}"
    ) from last_exc
