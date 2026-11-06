"""Which configured inference endpoints send text off the machine.

memtomem itself runs locally, but three optional components call an
inference service over HTTP: the embedding provider, the LLM provider
(``llm.enabled``) and the Cohere reranker. When that service is not on the
local machine, note text and queries leave it. This module answers "which
configured remote inference endpoints are there" for every surface that
discloses it (``mm init``, ``mem_status`` / ``mm status``, ``mm config set``,
the Web UI) and warns once per endpoint when a client first connects.

The base URL each client dials comes from the ``*_base_url`` resolvers below,
and the clients call the same resolvers, so the classification cannot drift
from what is actually dialled.

Scope, stated wherever the list is shown: this covers configured inference
endpoints only. Langfuse session tracing, webhooks, and content returned to
the connected MCP client (which the client's own model then processes) are
outside it, as are embeddings a caller computes itself and hands to the
LangGraph store.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from memtomem.config import EmbeddingConfig, LLMConfig, RerankConfig

logger = logging.getLogger(__name__)

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com"
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
COHERE_BASE_URL = "https://api.cohere.ai"

Component = Literal["embedding", "llm", "rerank"]

# What each component sends when it is active. Fixed phrases so every surface
# says the same thing.
SENDS: dict[Component, str] = {
    "embedding": "note text and search queries",
    "llm": "note text, queries and session notes for the enabled LLM features",
    "rerank": "search queries and the text of candidate results",
}

# Said next to every printed list, so no surface implies the list is complete.
NOT_COVERED = (
    "Not covered: Langfuse session tracing, webhooks, and content returned to "
    "the connected MCP client."
)

# Providers that never make a network call with note text.
_LOCAL_EMBEDDING_PROVIDERS = frozenset({"none", "onnx"})
_LOCAL_RERANK_PROVIDERS = frozenset({"local", "fastembed"})

# Network providers each factory builds. Anything else is classified remote:
# the factories reject unknown ids today, and a new one must not slip through
# as local before this module learns its default host.
_KNOWN_NETWORK_PROVIDERS: dict[Component, frozenset[str]] = {
    "embedding": frozenset({"ollama", "openai"}),
    "llm": frozenset({"ollama", "openai", "anthropic"}),
    "rerank": frozenset({"cohere"}),
}


@dataclass(frozen=True)
class InferenceEndpoint:
    """One configured inference endpoint of an active component."""

    component: Component
    provider: str
    host: str  # hostname[:port]; "" when the base URL has no host
    remote: bool
    sends: str

    def describe(self) -> str:
        """One line naming the component, provider, host and what is sent."""
        return f"{self.component} provider {self.provider} sends {self.sends} to {self.host}"


# ---------------------------------------------------------------------------
# Base URL resolvers — the clients call these for their httpx base_url.
# ---------------------------------------------------------------------------


def embedding_base_url(provider: str, base_url: str) -> str:
    """Base URL the embedding client for *provider* dials."""
    if provider.lower() == "openai":
        return (base_url or OPENAI_DEFAULT_BASE_URL).rstrip("/")
    return base_url or OLLAMA_DEFAULT_BASE_URL


def llm_base_url(provider: str, base_url: str) -> str:
    """Base URL the LLM client for *provider* dials.

    ``LLMConfig.base_url`` defaults to Ollama's address for every provider;
    the Anthropic client swaps that default for Anthropic's API. The OpenAI
    and Ollama clients use the value as given.
    """
    if provider.lower() == "anthropic" and base_url == OLLAMA_DEFAULT_BASE_URL:
        return ANTHROPIC_DEFAULT_BASE_URL
    return base_url


def rerank_base_url(provider: str) -> str:
    """Base URL the remote reranker dials (Cohere has no setting for it)."""
    return COHERE_BASE_URL


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _host_of(url: str) -> tuple[str, str | None]:
    """Return (display host, bare hostname) for *url*; hostname None if absent.

    Parsed with httpx, the parser the clients' ``base_url`` goes through, so a
    URL another parser rejects but httpx dials (``https://api.openai.com:+443``)
    is still classified by the host it reaches. A URL httpx rejects cannot be
    dialled at all.

    The host is read from ``raw_host``, the ASCII (punycode) form httpx sends:
    ``URL.host`` decodes IDNA and raises on a malformed label such as
    ``xn--a.example``, which would take the status report down with it.
    """
    import httpx

    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return "", None
    hostname = parsed.raw_host.decode("ascii", errors="replace").lower()
    port = parsed.port
    if not hostname:
        return "", None
    shown = f"[{hostname}]" if ":" in hostname else hostname
    return (f"{shown}:{port}" if port else shown), hostname


def is_loopback_host(hostname: str) -> bool:
    """True only for literal loopback names and addresses; no DNS lookup."""
    name = hostname.lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # Python 3.12 does not unwrap ::ffff:127.0.0.1 inside is_loopback.
        ip = ip.ipv4_mapped
    return ip.is_loopback


def classify_url(component: Component, provider: str, url: str) -> InferenceEndpoint:
    """Classify the endpoint *component* reaches at *url*.

    A base URL with no host cannot be dialled (the client fails before
    sending anything), so it is not remote. A provider id this module does
    not know is remote whatever its URL.
    """
    shown, hostname = _host_of(url)
    if provider not in _KNOWN_NETWORK_PROVIDERS[component]:
        remote = True
    else:
        remote = hostname is not None and not is_loopback_host(hostname)
    return InferenceEndpoint(
        component=component,
        provider=provider,
        host=shown,
        remote=remote,
        sends=SENDS[component],
    )


def describe_inference_endpoints(
    embedding: EmbeddingConfig,
    llm: LLMConfig,
    rerank: RerankConfig,
) -> list[InferenceEndpoint]:
    """Endpoints of the active components in the given (effective) config.

    Pure: no I/O, no DNS. Pass the effective config sections (file + env).
    Unknown provider ids are classified remote.
    """
    endpoints: list[InferenceEndpoint] = []

    emb_provider = embedding.provider.lower()
    if emb_provider not in _LOCAL_EMBEDDING_PROVIDERS:
        endpoints.append(
            classify_url(
                "embedding",
                emb_provider,
                embedding_base_url(emb_provider, embedding.base_url),
            )
        )

    if llm.enabled:
        llm_provider = llm.provider.lower()
        endpoints.append(
            classify_url("llm", llm_provider, llm_base_url(llm_provider, llm.base_url))
        )

    if rerank.enabled:
        rr_provider = rerank.provider.lower()
        if rr_provider not in _LOCAL_RERANK_PROVIDERS:
            endpoints.append(classify_url("rerank", rr_provider, rerank_base_url(rr_provider)))

    return endpoints


def remote_endpoints(
    embedding: EmbeddingConfig,
    llm: LLMConfig,
    rerank: RerankConfig,
) -> list[InferenceEndpoint]:
    """The remote subset of :func:`describe_inference_endpoints`."""
    return [ep for ep in describe_inference_endpoints(embedding, llm, rerank) if ep.remote]


# ---------------------------------------------------------------------------
# Runtime announcement
# ---------------------------------------------------------------------------

_announced: set[tuple[str, str, str]] = set()
_announced_lock = threading.Lock()


def announce_remote_endpoint(endpoint: InferenceEndpoint) -> None:
    """Warn once per process that *endpoint* sends text off the machine.

    Called by each remote-capable client right before it creates its HTTP
    client, which happens immediately before its first request. Building a
    provider and discarding it (a rejected settings change) never gets here.
    Logs go to stderr; MCP stdio uses stdout.
    """
    if not endpoint.remote:
        return
    key = (endpoint.component, endpoint.provider, endpoint.host)
    with _announced_lock:
        if key in _announced:
            return
        _announced.add(key)
    logger.warning("Remote inference: %s", endpoint.describe())


def announce_url(component: Component, provider: str, url: str) -> None:
    """Classify *url* and announce it when remote (client-side helper)."""
    announce_remote_endpoint(classify_url(component, provider, url))


def _reset_announced_for_tests() -> set[tuple[str, str, str]]:
    """Clear the dedup set and return its previous contents."""
    with _announced_lock:
        previous = set(_announced)
        _announced.clear()
    return previous


def _restore_announced_for_tests(previous: set[tuple[str, str, str]]) -> None:
    with _announced_lock:
        _announced.clear()
        _announced.update(previous)
