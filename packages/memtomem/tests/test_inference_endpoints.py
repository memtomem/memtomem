"""Classifying configured inference endpoints and announcing remote ones."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import httpx
import pytest

from memtomem import inference_endpoints as ie
from memtomem.config import EmbeddingConfig, LLMConfig, RerankConfig

SRC = Path(ie.__file__).parent
LOGGER = "memtomem.inference_endpoints"


def _endpoints(
    embedding: EmbeddingConfig | None = None,
    llm: LLMConfig | None = None,
    rerank: RerankConfig | None = None,
) -> list[ie.InferenceEndpoint]:
    return ie.describe_inference_endpoints(
        embedding or EmbeddingConfig(), llm or LLMConfig(), rerank or RerankConfig()
    )


def _announced(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.getMessage().startswith("Remote inference:")
    ]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_config_has_no_endpoints(self):
        assert _endpoints() == []

    @pytest.mark.parametrize("provider", ["none", "onnx", "NONE", "Onnx"])
    def test_local_embedding_providers_have_no_endpoint(self, provider):
        assert _endpoints(EmbeddingConfig(provider=provider)) == []

    def test_disabled_llm_has_no_endpoint_even_when_remote(self):
        llm = LLMConfig(enabled=False, provider="openai", base_url="https://api.openai.com")
        assert _endpoints(llm=llm) == []

    def test_disabled_cohere_rerank_has_no_endpoint(self):
        assert _endpoints(rerank=RerankConfig(enabled=False, provider="cohere")) == []

    @pytest.mark.parametrize("provider", ["fastembed", "local"])
    def test_local_rerankers_have_no_endpoint(self, provider):
        assert _endpoints(rerank=RerankConfig(enabled=True, provider=provider)) == []


@pytest.mark.parametrize(
    ("embedding", "host", "remote"),
    [
        (EmbeddingConfig(provider="openai"), "api.openai.com", True),
        (EmbeddingConfig(provider="openai", base_url=""), "api.openai.com", True),
        (EmbeddingConfig(provider="OpenAI"), "api.openai.com", True),
        (
            EmbeddingConfig(provider="openai", base_url="http://localhost:8000"),
            "localhost:8000",
            False,
        ),
        (
            EmbeddingConfig(provider="openai", base_url="https://gw.example.com/"),
            "gw.example.com",
            True,
        ),
        (EmbeddingConfig(provider="ollama"), "localhost:11434", False),
        (EmbeddingConfig(provider="ollama", base_url=""), "localhost:11434", False),
        (
            EmbeddingConfig(provider="ollama", base_url="http://gpu-box.lan:11434"),
            "gpu-box.lan:11434",
            True,
        ),
        (
            EmbeddingConfig(provider="ollama", base_url="http://[::1]:11434"),
            "[::1]:11434",
            False,
        ),
        (
            EmbeddingConfig(provider="ollama", base_url="http://127.0.0.2:11434"),
            "127.0.0.2:11434",
            False,
        ),
        (
            EmbeddingConfig(provider="ollama", base_url="http://[::ffff:127.0.0.1]:11434"),
            "[::ffff:127.0.0.1]:11434",
            False,
        ),
        (
            EmbeddingConfig(provider="ollama", base_url="http://10.0.0.5:11434"),
            "10.0.0.5:11434",
            True,
        ),
    ],
)
def test_embedding_classification(embedding, host, remote):
    [ep] = _endpoints(embedding)
    assert (ep.component, ep.host, ep.remote) == ("embedding", host, remote)
    assert ep.sends == ie.SENDS["embedding"]


@pytest.mark.parametrize(
    ("llm", "host", "remote"),
    [
        # LLMConfig.base_url defaults to Ollama's address for every provider.
        (LLMConfig(enabled=True, provider="ollama"), "localhost:11434", False),
        (LLMConfig(enabled=True, provider="openai"), "localhost:11434", False),
        (LLMConfig(enabled=True, provider="anthropic"), "api.anthropic.com", True),
        (
            LLMConfig(enabled=True, provider="openai", base_url="https://api.openai.com"),
            "api.openai.com",
            True,
        ),
        (
            LLMConfig(enabled=True, provider="openai", base_url="http://127.0.0.1:8000"),
            "127.0.0.1:8000",
            False,
        ),
        (
            LLMConfig(enabled=True, provider="anthropic", base_url="https://proxy.example.com"),
            "proxy.example.com",
            True,
        ),
        (
            LLMConfig(enabled=True, provider="ollama", base_url="http://ollama.example.com"),
            "ollama.example.com",
            True,
        ),
        # An empty base_url cannot be dialled by these clients: nothing is sent.
        (LLMConfig(enabled=True, provider="openai", base_url=""), "", False),
        (LLMConfig(enabled=True, provider="anthropic", base_url=""), "", False),
    ],
)
def test_llm_classification(llm, host, remote):
    [ep] = _endpoints(llm=llm)
    assert (ep.component, ep.host, ep.remote) == ("llm", host, remote)


def test_cohere_rerank_is_remote():
    [ep] = _endpoints(rerank=RerankConfig(enabled=True, provider="cohere"))
    assert (ep.component, ep.provider, ep.host, ep.remote) == (
        "rerank",
        "cohere",
        "api.cohere.ai",
        True,
    )


@pytest.mark.parametrize(
    ("component", "url"),
    [
        ("embedding", "http://localhost:11434"),
        ("llm", "http://localhost:11434"),
        ("rerank", "http://localhost:9000"),
    ],
)
def test_unknown_provider_is_remote_even_on_loopback(component, url):
    ep = ie.classify_url(component, "brand-new", url)
    assert ep.remote is True


def test_all_three_components_listed_in_order():
    eps = _endpoints(
        EmbeddingConfig(provider="openai"),
        LLMConfig(enabled=True, provider="anthropic"),
        RerankConfig(enabled=True, provider="cohere"),
    )
    assert [(e.component, e.host) for e in eps] == [
        ("embedding", "api.openai.com"),
        ("llm", "api.anthropic.com"),
        ("rerank", "api.cohere.ai"),
    ]
    assert eps == ie.remote_endpoints(
        EmbeddingConfig(provider="openai"),
        LLMConfig(enabled=True, provider="anthropic"),
        RerankConfig(enabled=True, provider="cohere"),
    )


@pytest.mark.parametrize(
    "hostname",
    ["localhost", "LOCALHOST", "localhost.", "api.localhost", "127.0.0.1", "127.9.9.9", "::1"],
)
def test_loopback_literals(hostname):
    assert ie.is_loopback_host(hostname)


@pytest.mark.parametrize(
    "hostname", ["0.0.0.0", "10.0.0.1", "192.168.1.2", "api.openai.com", "localhost.example.com"]
)
def test_not_loopback(hostname):
    assert not ie.is_loopback_host(hostname)


# ---------------------------------------------------------------------------
# Registry parity: every provider id the factories accept is classified.
# ---------------------------------------------------------------------------


def _provider_literals(factory: Path) -> set[str]:
    """String literals compared against ``provider`` in a factory module."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(factory.read_text())):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "provider"
        ):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    found.add(comp.value)
    return found


@pytest.mark.parametrize(
    ("component", "factory", "local"),
    [
        ("embedding", "embedding/factory.py", ie._LOCAL_EMBEDDING_PROVIDERS),
        ("llm", "llm/factory.py", frozenset()),
        ("rerank", "search/reranker/factory.py", ie._LOCAL_RERANK_PROVIDERS),
    ],
)
def test_every_factory_provider_is_classified(component, factory, local):
    accepted = _provider_literals(SRC / factory)
    assert accepted, f"no provider literals found in {factory}"
    classified = local | ie._KNOWN_NETWORK_PROVIDERS[component]
    assert accepted == classified


# ---------------------------------------------------------------------------
# Clients: the resolver is what they dial, and they announce at connect time.
# ---------------------------------------------------------------------------


def _embedder(provider: str, base_url: str):
    from memtomem.embedding.factory import create_embedder

    return create_embedder(EmbeddingConfig(provider=provider, base_url=base_url, model="m"))


def _llm(provider: str, base_url: str):
    from memtomem.llm.factory import create_llm

    return create_llm(LLMConfig(enabled=True, provider=provider, base_url=base_url))


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", "https://api.openai.com/v1/embeddings"),
        ("https://gw.example.com", "https://gw.example.com/v1/embeddings"),
        ("https://gw.example.com/", "https://gw.example.com/v1/embeddings"),
        ("https://gw.example.com///", "https://gw.example.com/v1/embeddings"),
        ("https://gw.example.com/prefix/", "https://gw.example.com/prefix/v1/embeddings"),
        # The base URL as OpenAI, vLLM, LM Studio and OpenRouter print it.
        ("https://api.openai.com/v1", "https://api.openai.com/v1/embeddings"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1/embeddings"),
        ("http://localhost:1234/v1", "http://localhost:1234/v1/embeddings"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/embeddings"),
        # Only one segment goes, so a server under /prefix/v1/v1 stays reachable.
        ("https://gw.example.com/p/v1/v1", "https://gw.example.com/p/v1/v1/embeddings"),
    ],
)
async def test_openai_embedder_request_url(configured, expected):
    # (``base_url`` would collide with the pytest-base-url plugin's fixture.)
    emb = _embedder("openai", configured)
    client = emb._get_client()
    try:
        assert str(client.build_request("POST", "/v1/embeddings").url) == expected
    finally:
        await emb.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1/chat/completions"),
        ("http://localhost:8000/v1", "http://localhost:8000/v1/chat/completions"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/chat/completions"),
        ("https://openrouter.ai/api", "https://openrouter.ai/api/v1/chat/completions"),
    ],
)
async def test_openai_llm_request_url(configured, expected):
    llm = _llm("openai", configured)
    client = llm._get_client()
    try:
        assert str(client.build_request("POST", "/v1/chat/completions").url) == expected
    finally:
        await llm.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://h.example/v1", "https://h.example"),
        ("https://h.example/v1//", "https://h.example"),
        ("https://h.example/api/v1", "https://h.example/api"),
        ("https://[::1]:8000/v1", "https://[::1]:8000"),
        ("https://u:p%40@h.example/v1", "https://u:p%40@h.example"),
        # The encoded path is kept as written: a decoded rebuild would turn
        # %2F into a separator and raise on %3F / %23.
        ("https://h.example/a%2Fb/v1", "https://h.example/a%2Fb"),
        ("https://h.example/a%3Fb/v1", "https://h.example/a%3Fb"),
        ("https://h.example/a%23b/v1", "https://h.example/a%23b"),
        ("https://h.example/v1?x=1", "https://h.example?x=1"),
    ],
)
def test_strip_trailing_v1_drops_one_path_segment(configured, expected):
    assert ie._strip_trailing_v1(configured) == expected


@pytest.mark.parametrize(
    "configured",
    [
        "",
        "not a url",
        "http://v1",  # host, not path
        "http://v1/",
        "https://h.example",
        "https://h.example/",
        "https://h.example/V1",
        "https://h.example/v1beta",
        "https://h.example/xv1",
        "https://h.example/v1/x",
        "https://h.example/api%2Fv1",  # encoded slash is not a segment boundary
        "https://h.example/api?route=/v1",  # query, not path
        "http://[::1",  # httpx rejects it
    ],
)
def test_strip_trailing_v1_leaves_other_urls_byte_identical(configured):
    assert ie._strip_trailing_v1(configured) == configured


_GW_QUERY = "https://gw.example.com/openai?api-version=2024-10-21"


@pytest.mark.parametrize(
    ("kind", "provider", "endpoint"),
    [
        ("embedding", "openai", "/v1/embeddings"),
        ("embedding", "ollama", "/api/embed"),
        ("llm", "openai", "/v1/chat/completions"),
        ("llm", "ollama", "/api/generate"),
        ("llm", "anthropic", "/v1/messages"),
    ],
)
async def test_query_in_base_url_stays_after_the_endpoint(kind, provider, endpoint):
    obj = _embedder(provider, _GW_QUERY) if kind == "embedding" else _llm(provider, _GW_QUERY)
    client = obj._get_client()
    try:
        url = str(client.build_request("POST", endpoint).url)
        assert url == f"https://gw.example.com/openai{endpoint}?api-version=2024-10-21"
    finally:
        await obj.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # Composes with the trailing-/v1 strip.
        ("https://gw.example.com/v1?x=1", "https://gw.example.com/v1/embeddings?x=1"),
        ("https://gw.example.com/v1/?x=1", "https://gw.example.com/v1/embeddings?x=1"),
        # A slash that ends a query value is not a path slash.
        ("https://gw.example.com/v1?key=abc/", "https://gw.example.com/v1/embeddings?key=abc%2F"),
        ("https://gw.example.com?key=abc%2F", "https://gw.example.com/v1/embeddings?key=abc%2F"),
        ("https://gw.example.com/p/?key=abc/", "https://gw.example.com/p/v1/embeddings?key=abc%2F"),
        # A bare trailing "?" would otherwise swallow the endpoint too.
        ("https://gw.example.com/p?", "https://gw.example.com/p/v1/embeddings"),
    ],
)
async def test_openai_embedder_query_request_url(configured, expected):
    emb = _embedder("openai", configured)
    client = emb._get_client()
    try:
        assert str(client.build_request("POST", "/v1/embeddings").url) == expected
    finally:
        await emb.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://gw.example.com/v1?x=1", "https://gw.example.com/v1/chat/completions?x=1"),
        (
            "https://gw.example.com/v1?key=abc/",
            "https://gw.example.com/v1/chat/completions?key=abc%2F",
        ),
        ("https://gw.example.com/p?", "https://gw.example.com/p/v1/chat/completions"),
    ],
)
async def test_openai_llm_query_request_url(configured, expected):
    llm = _llm("openai", configured)
    client = llm._get_client()
    try:
        assert str(client.build_request("POST", "/v1/chat/completions").url) == expected
    finally:
        await llm.close()


@pytest.mark.parametrize(
    "configured",
    ["", "not a url", "https://h.example", "https://h.example/p/", "http://[::1"],
)
def test_split_query_leaves_urls_without_a_query_alone(configured):
    assert ie.split_query(configured) == (configured, None)


def test_split_query_bare_question_mark_has_no_params():
    assert ie.split_query("https://h.example/p?") == ("https://h.example/p", None)


def test_split_query_keeps_repeated_keys_and_their_order():
    base, params = ie.split_query("https://h.example/p?a=1&b=2&a=3")
    assert base == "https://h.example/p"
    assert params is not None
    assert params.get_list("a") == ["1", "3"]
    assert params.get_list("b") == ["2"]


def test_split_query_reencodes_the_query_as_form_params():
    # Characterises httpx's re-serialisation, so a change to it is visible.
    _, params = ie.split_query("https://h.example/p?flag&c=x%20y")
    assert str(params) == "flag=&c=x+y"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://h.example///", "https://h.example"),
        ("https://h.example/p/?k=a/", "https://h.example/p?k=a/"),
        ("https://h.example/p//#f", "https://h.example/p#f"),
    ],
)
def test_trim_path_slashes_touches_only_the_path(configured, expected):
    assert ie._trim_path_slashes(configured) == expected


@pytest.mark.parametrize(
    "configured",
    ["", "not a url", "https://h.example", "https://h.example/", "https://h.example/p?k=a/"],
)
def test_trim_path_slashes_leaves_other_urls_alone(configured):
    assert ie._trim_path_slashes(configured) == configured


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
def test_trailing_v1_kept_for_non_openai_llm(provider):
    assert ie.llm_base_url(provider, "https://gw.example.com/v1") == "https://gw.example.com/v1"


def test_trailing_v1_kept_for_ollama_embedding():
    url = "http://gpu-box.lan:11434/v1"
    assert ie.embedding_base_url("ollama", url) == url


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_web_guide_base_url_example_has_no_v1(lang):
    import json

    locale = json.loads((SRC / "web" / "static" / "locales" / f"{lang}.json").read_text("utf-8"))
    text = locale["settings.config.guide.embedding.item.base_url.text"]
    assert "https://api.openai.com" in text
    assert "api.openai.com/v1" not in text


@pytest.mark.parametrize(
    ("kind", "provider", "configured"),
    [
        ("embedding", "openai", ""),
        ("embedding", "openai", "https://gw.example.com//"),
        ("embedding", "ollama", ""),
        ("embedding", "ollama", "http://gpu-box.lan:11434"),
        ("embedding", "openai", "https://api.openai.com/v1"),
        ("llm", "openai", "https://api.openai.com"),
        ("llm", "openai", "http://localhost:1234/v1/"),
        ("llm", "ollama", "http://localhost:11434"),
        ("llm", "anthropic", "http://localhost:11434"),
        ("llm", "anthropic", "https://proxy.example.com"),
        ("embedding", "openai", _GW_QUERY),
        ("embedding", "ollama", _GW_QUERY),
        ("llm", "openai", "https://gw.example.com/v1?x=1"),
        ("llm", "ollama", _GW_QUERY),
        ("llm", "anthropic", _GW_QUERY),
    ],
)
async def test_client_base_url_matches_resolver(kind, provider, configured):
    if kind == "embedding":
        obj = _embedder(provider, configured)
        resolved = ie.embedding_base_url(provider, configured)
    else:
        obj = _llm(provider, configured)
        resolved = ie.llm_base_url(provider, configured)
    base, params = ie.split_query(resolved)
    client = obj._get_client()
    try:
        assert str(client.base_url).rstrip("/") == base.rstrip("/")
        assert client.params == (params or httpx.QueryParams())
    finally:
        await obj.close()


async def test_cohere_posts_to_resolver_host(monkeypatch):
    from memtomem.search.reranker.cohere import CohereReranker

    from .test_reranker import _make_result

    reranker = CohereReranker(RerankConfig(enabled=True, provider="cohere", api_key="k"))
    seen: list[str] = []

    async def fake_post(url, **kwargs):
        seen.append(url)
        raise RuntimeError("stop")

    client = reranker._get_client()
    monkeypatch.setattr(client, "post", fake_post)
    try:
        await reranker.rerank("q", [_make_result("a", 1.0)], top_k=1)
    finally:
        await reranker.close()
    assert seen == [f"{ie.rerank_base_url('cohere')}/v1/rerank"]


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (
            lambda: _embedder("openai", ""),
            "Remote inference: embedding provider openai sends note text and search "
            "queries to api.openai.com",
        ),
        (
            lambda: _embedder("ollama", "http://gpu-box.lan:11434"),
            "Remote inference: embedding provider ollama sends note text and search "
            "queries to gpu-box.lan:11434",
        ),
        (
            lambda: _llm("anthropic", "http://localhost:11434"),
            f"Remote inference: llm provider anthropic sends {ie.SENDS['llm']} "
            "to api.anthropic.com",
        ),
        (
            lambda: _llm("openai", "https://api.openai.com"),
            f"Remote inference: llm provider openai sends {ie.SENDS['llm']} to api.openai.com",
        ),
        (
            lambda: _llm("ollama", "http://ollama.example.com"),
            f"Remote inference: llm provider ollama sends {ie.SENDS['llm']} to ollama.example.com",
        ),
        (
            lambda: __import__(
                "memtomem.search.reranker.cohere", fromlist=["CohereReranker"]
            ).CohereReranker(RerankConfig(enabled=True, provider="cohere", api_key="k")),
            f"Remote inference: rerank provider cohere sends {ie.SENDS['rerank']} to api.cohere.ai",
        ),
    ],
)
async def test_remote_client_announces_on_first_connect_only(caplog, make, expected):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    obj = make()
    assert _announced(caplog) == [], "construction alone must not announce"
    obj._get_client()
    obj._get_client()
    try:
        assert _announced(caplog) == [expected]
        assert all(r.levelno == logging.WARNING for r in caplog.records if r.name == LOGGER)
    finally:
        await obj.close()


@pytest.mark.parametrize(
    "make",
    [
        lambda: _embedder("ollama", ""),
        lambda: _embedder("openai", "http://localhost:8000"),
        lambda: _llm("ollama", "http://localhost:11434"),
        lambda: _llm("openai", "http://127.0.0.1:8000"),
    ],
)
async def test_local_client_announces_nothing(caplog, make):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    obj = make()
    obj._get_client()
    try:
        assert _announced(caplog) == []
    finally:
        await obj.close()


async def test_dedup_across_instances_and_new_host_announces_again(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    first = _embedder("openai", "")
    second = _embedder("openai", "https://api.openai.com/")
    other = _embedder("openai", "https://gw.example.com")
    try:
        first._get_client()
        second._get_client()
        assert len(_announced(caplog)) == 1
        other._get_client()
        assert _announced(caplog)[-1].endswith("to gw.example.com")
        assert len(_announced(caplog)) == 2
    finally:
        for obj in (first, second, other):
            await obj.close()


async def test_discarded_provider_does_not_spend_the_announcement(caplog):
    """A provider built and discarded (rejected settings change) never connects,
    so the later real activation still announces — exactly once."""
    from memtomem.search.reranker.cohere import CohereReranker

    caplog.set_level(logging.WARNING, logger=LOGGER)
    cfg = RerankConfig(enabled=True, provider="cohere", api_key="k")
    candidate = CohereReranker(cfg)
    await candidate.close()
    assert _announced(caplog) == []

    live = CohereReranker(cfg)
    live._get_client()
    live._get_client()
    try:
        assert len(_announced(caplog)) == 1
    finally:
        await live.close()


def test_announce_is_thread_safe(caplog):
    import threading

    caplog.set_level(logging.WARNING, logger=LOGGER)
    ep = ie.classify_url("embedding", "openai", "https://api.openai.com")
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        ie.announce_remote_endpoint(ep)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_announced(caplog)) == 1


@pytest.mark.parametrize(
    "configured", ["https://api.openai.com:+443", "https://api.openai.com:０443"]
)
async def test_port_spelling_only_httpx_accepts_is_still_remote(caplog, configured):
    """``urllib`` rejects these ports; httpx normalizes them and dials OpenAI."""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    [ep] = _endpoints(EmbeddingConfig(provider="openai", base_url=configured))
    assert (ep.host, ep.remote) == ("api.openai.com", True)

    emb = _embedder("openai", configured)
    client = emb._get_client()
    try:
        assert client.build_request("POST", "/v1/embeddings").url.host == "api.openai.com"
        assert _announced(caplog) == [
            "Remote inference: embedding provider openai sends note text and search "
            "queries to api.openai.com"
        ]
    finally:
        await emb.close()


def test_url_httpx_rejects_is_not_remote():
    """httpx refuses it at client construction, so nothing can be sent."""
    import httpx

    url = "https://[zz]/"
    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient(base_url=url)
    ep = ie.classify_url("embedding", "openai", url)
    assert (ep.host, ep.remote) == ("", False)


@pytest.mark.parametrize(
    ("configured", "host"),
    [
        # httpx accepts this URL, but decoding the label raises; the classifier
        # must not (the status report and the Web route call it).
        ("https://xn--a.example", "xn--a.example"),
        ("https://bücher.example", "xn--bcher-kva.example"),
        ("https://API.OpenAI.com", "api.openai.com"),
    ],
)
def test_host_is_the_ascii_form_httpx_sends(configured, host):
    [ep] = _endpoints(EmbeddingConfig(provider="openai", base_url=configured))
    assert (ep.host, ep.remote) == (host, True)
