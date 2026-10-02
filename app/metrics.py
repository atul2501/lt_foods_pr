"""Prometheus metrics, served by the API process at /metrics. Each process has its own
registry, so the Ollama counters only cover calls made inside the API process (POST
/api/v1/invoices); the email poller's extractions show up in its logs instead.
"""
from prometheus_client import Counter, Gauge

RESULTS_PENDING = Gauge("invoice_results_pending", "Extracted results not yet fetched via /api/v1/invoices/new")

# Proxy for Ollama Cloud spend, which bills on usage - a sustained jump in request rate or
# tokens/request is the earliest signal of a cost blowout, well before a bill arrives.
OLLAMA_REQUESTS = Counter(
    "invoice_ollama_requests_total",
    "Ollama generate calls, by outcome",
    ["outcome"],
)
OLLAMA_EVAL_TOKENS = Counter(
    "invoice_ollama_eval_tokens_total",
    "Sum of eval_count (output tokens) reported by Ollama across all requests",
)
