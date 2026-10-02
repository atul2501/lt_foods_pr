import itertools
import json
import time
import httpx
from app.config import settings
from app.core.exceptions import LLMFormatError
from app.logging_conf import get_logger
from app.metrics import OLLAMA_EVAL_TOKENS, OLLAMA_REQUESTS

logger = get_logger(__name__)


class OllamaClient:
    """Thin wrapper around the Ollama REST API - self-hosted, Ollama Cloud
    (https://ollama.com), or a round-robin mix of self-hosted replicas.

    Round-robins across `settings.ollama_hosts` so a second/third instance
    can be added purely via config once one becomes the throughput
    bottleneck - no code change needed. Set `settings.ollama_api_key` to
    switch a host to Ollama Cloud (sent as an `Authorization: Bearer` header
    on every request); leave it unset to stay fully self-hosted.
    """

    def __init__(self, hosts: list[str] | None = None, model: str | None = None, api_key: str | None = None):
        self._hosts = hosts or [h.strip() for h in settings.ollama_hosts.split(",") if h.strip()]
        if not self._hosts:
            raise ValueError("no Ollama hosts configured")
        self._host_cycle = itertools.cycle(self._hosts)
        self.model = model or settings.ollama_model
        self._api_key = api_key if api_key is not None else settings.ollama_api_key

    def _next_host(self) -> str:
        return next(self._host_cycle)

    def generate_structured(self, prompt: str, json_schema: dict, *, temperature: float = 0.0) -> dict:
        host = self._next_host()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "format": json_schema,
            "stream": False,
            "options": {
                "temperature": temperature,
                # Ollama silently truncates context to a small default otherwise - this is
                # what prevents long, many-line-item invoices from looking like "missing data".
                "num_ctx": settings.ollama_num_ctx,
            },
        }

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        logger.info(
            "ollama_request_started",
            host=host,
            model=self.model,
            prompt_chars=len(prompt),
            num_ctx=settings.ollama_num_ctx,
            authenticated=bool(self._api_key),
        )
        started = time.monotonic()
        try:
            with httpx.Client(timeout=settings.ollama_timeout_seconds) as client:
                response = client.post(f"{host}/api/generate", json=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            # HTTPStatusError's default message omits the response body, which is where
            # Ollama Cloud puts the actual reason for a 401/403 (e.g. bad/missing API key).
            detail = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            logger.error(
                "ollama_request_failed",
                host=host,
                model=self.model,
                error=detail,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            OLLAMA_REQUESTS.labels(outcome="error").inc()
            raise LLMFormatError(f"Ollama request failed ({host}): {detail}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        body = response.json()
        raw = body.get("response", "")
        eval_count = body.get("eval_count")
        logger.info(
            "ollama_request_complete",
            host=host,
            model=self.model,
            response_chars=len(raw),
            eval_count=eval_count,
            eval_duration_ns=body.get("eval_duration"),
            duration_ms=duration_ms,
        )
        OLLAMA_REQUESTS.labels(outcome="success").inc()
        if eval_count:
            OLLAMA_EVAL_TOKENS.inc(eval_count)

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("ollama_response_not_json", host=host, model=self.model, error=str(exc))
            raise LLMFormatError(f"Ollama did not return valid JSON: {exc}") from exc
