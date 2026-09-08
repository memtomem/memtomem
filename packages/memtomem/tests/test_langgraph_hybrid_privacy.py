"""Gate A covers persisted identifiers as well as payloads (#2374)."""

import inspect
import json
import sqlite3

import pytest

pytest.importorskip("langgraph")
from langgraph.store.base import PutOp

from memtomem import privacy
from memtomem.integrations import MemtomemHybridStore

TOKEN = "AKIA" + "A" * 16  # Synthetic pattern fixture, never a credential.
RAW_SECRETS = ['{"password": "synthetic-example"}', "BEGIN\nRSA\nPRIVATE\nKEY"]


@pytest.fixture
def scoped_path(tmp_path, monkeypatch):
    shared = tmp_path / "project" / ".memtomem" / "memories"
    local = shared.with_name("memories.local")
    monkeypatch.setenv(
        "MEMTOMEM_INDEXING__PROJECT_MEMORY_DIRS", json.dumps([str(shared), str(local)])
    )
    return {
        "user": tmp_path / "private" / "store.db",
        "project_local": local / "store.db",
        "project_shared": shared / "store.db",
    }


def operation(field, secret=TOKEN):
    values = dict(namespace=("docs",), key="key", value={"text": "apple"}, index=None)
    values[field] = {
        "namespace": ("docs", secret),
        "key": secret,
        "value": {"text": secret},
        "index": [secret],
    }[field]
    return PutOp(**values)


def envelope(*ops):
    return {
        "format": "memtomem-hybrid-json-v1",
        "records": [
            {
                **op._asdict(),
                "created_at": 1.0,
                "updated_at": 1.0,
                "revision": 1,
                "expires_at": None,
            }
            for op in ops
        ],
    }


async def write(store, api, op):
    if api in {"batch", "abatch"}:
        result = getattr(store, api)([op])
    elif api in {"import_json", "aimport_json"}:
        result = getattr(store, api)(envelope(op))
    else:
        result = getattr(store, api)(op.namespace, op.key, op.value, index=op.index)
    if inspect.isawaitable(result):
        await result


def database_state(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("items", "item_fts", "vectors", "hybrid_meta")
        }


@pytest.mark.parametrize("api", ["put", "aput", "batch", "abatch", "import_json", "aimport_json"])
@pytest.mark.parametrize("field", ["namespace", "key", "index", "value"])
@pytest.mark.parametrize("scope", ["user", "project_local", "project_shared"])
@pytest.mark.parametrize("force", [False, True])
async def test_record_guard_all_ingress(scoped_path, caplog, api, field, scope, force):
    path = scoped_path[scope]
    op = operation(field)
    allowed = force and scope != "project_shared"
    with MemtomemHybridStore(path, confirm_project_shared=True, force_unsafe=force) as store:
        assert store.scope == scope
        before = privacy.snapshot()["by_tool"].get("langgraph_hybridstore_put", {})
        if allowed:
            await write(store, api, op)
            assert store.get(op.namespace, op.key) is not None
        else:
            with pytest.raises(ValueError, match="Store write blocked by privacy guard") as error:
                await write(store, api, op)
            assert TOKEN not in str(error.value)
            assert store.export_json()["records"] == []
            assert all(
                not database_state(path)[table] for table in ("items", "item_fts", "vectors")
            )
        decision = "bypassed" if allowed else "blocked_project_shared" if force else "blocked"
        after = privacy.snapshot()["by_tool"]["langgraph_hybridstore_put"]
        assert after[decision] - before.get(decision, 0) == 1
        assert after["pass"] == before.get("pass", 0)
    assert TOKEN not in caplog.text
    if not allowed:
        assert all(TOKEN.encode() not in p.read_bytes() for p in path.parent.glob(path.name + "*"))


@pytest.mark.parametrize("field", ["namespace", "key", "index"])
@pytest.mark.parametrize("secret", RAW_SECRETS)
async def test_identifiers_scan_raw_strings(scoped_path, caplog, field, secret):
    with MemtomemHybridStore(scoped_path["project_shared"], confirm_project_shared=True) as store:
        with pytest.raises(ValueError, match="privacy") as error:
            await write(store, "aput", operation(field, secret))
        assert secret not in str(error.value)
        assert not store.export_json()["records"]
    assert secret not in caplog.text


@pytest.mark.parametrize("field", ["fields", "index_id"])
@pytest.mark.parametrize("secret", [TOKEN, *RAW_SECRETS])
@pytest.mark.parametrize("scope", ["user", "project_local", "project_shared"])
@pytest.mark.parametrize("force", [False, True])
def test_configuration_guard_before_creation(scoped_path, caplog, field, secret, scope, force):
    path = scoped_path[scope]
    options = {"index": {"fields": [secret]}} if field == "fields" else {"index_id": secret}
    allowed = force and scope != "project_shared"
    if allowed:
        with MemtomemHybridStore(path, confirm_project_shared=True, force_unsafe=force, **options):
            assert secret in json.loads(dict(database_state(path)["hybrid_meta"])["index"])[field]
    else:
        with pytest.raises(
            ValueError, match="Store configuration blocked by privacy guard"
        ) as error:
            MemtomemHybridStore(path, confirm_project_shared=True, force_unsafe=force, **options)
        assert secret not in str(error.value)
        assert not path.parent.exists()
    assert secret not in caplog.text


def test_reopen_bad_configuration_leaves_existing_database_untouched(tmp_path):
    path = tmp_path / "store.db"
    with MemtomemHybridStore(path, index_id=TOKEN, force_unsafe=True) as store:
        store.put(("docs",), "existing", {"text": "apple"})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="configuration blocked"):
        MemtomemHybridStore(path, index_id=TOKEN)
    assert path.read_bytes() == before
    with MemtomemHybridStore(path, index_id=TOKEN, force_unsafe=True) as store:
        assert store.get(("docs",), "existing")


def test_blocked_configuration_does_not_initialize_embedder(tmp_path, monkeypatch):
    import memtomem.integrations.langgraph_hybrid_store as adapter

    def forbidden(*args):
        pytest.fail("Embedder initialized before Gate A")

    monkeypatch.setattr(adapter, "ensure_embeddings", forbidden)
    with pytest.raises(ValueError, match="configuration blocked"):
        MemtomemHybridStore(
            tmp_path / "new" / "store.db",
            index={"embed": forbidden, "dims": 2},
            index_id=TOKEN,
        )
    assert not (tmp_path / "new").exists()


def test_blocked_update_preserves_value_indexes_and_never_embeds(scoped_path):
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for text in texts]

    path = scoped_path["project_shared"]
    with MemtomemHybridStore(
        path,
        confirm_project_shared=True,
        index={"embed": embed, "dims": 2, "fields": ["text"]},
        index_id="toy-v1",
    ) as store:
        store.put(("docs",), "key", {"text": "apple"})
        before = database_state(path)
        with pytest.raises(ValueError, match="privacy"):
            store.put(("docs",), "key", {"text": "pear"}, index=["text", TOKEN])
        assert database_state(path) == before
        assert calls == [["apple"]]
        assert store.search((), query="apple")[0].value == {"text": "apple"}


@pytest.mark.parametrize("api", ["import_json", "aimport_json", "batch", "abatch"])
async def test_later_refusal_preserves_import_and_batch_contracts(tmp_path, api):
    first = PutOp(("docs",), "first", {"text": "apple"})
    bad = operation("key")
    with MemtomemHybridStore(tmp_path / "store.db") as store:
        store.put(("docs",), "existing", {"text": "pear"})
        before = store.export_json()
        with pytest.raises(ValueError, match="privacy"):
            result = getattr(store, api)(envelope(first, bad) if "import" in api else [first, bad])
            if inspect.isawaitable(result):
                await result
        assert store.get(bad.namespace, bad.key) is None
        if "import" in api:
            assert store.export_json() == before
        else:
            assert store.get(first.namespace, first.key)
            assert store.get(("docs",), "existing")


def test_embedding_callback_cannot_change_scanned_inputs(tmp_path):
    selectors, value = ["text"], {"text": "apple"}
    fields = ["text"]

    def embed(texts):
        selectors[:] = [TOKEN]
        fields[:] = [TOKEN]
        value["text"] = TOKEN
        assert texts == ["apple"]
        return [[1.0, 0.0]]

    with MemtomemHybridStore(
        tmp_path / "store.db", index={"embed": embed, "dims": 2, "fields": fields}, index_id="toy"
    ) as store:
        store.put(("docs",), "key", value, index=selectors)
        exported = store.export_json()["records"][0]
        assert exported["index"] == ["text"]
        assert exported["value"] == {"text": "apple"}
        assert TOKEN not in repr(database_state(store.path))


def test_legacy_identifiers_can_be_read_and_deleted(scoped_path):
    path = scoped_path["project_shared"]
    # Simulate a database created before #2374 without weakening the live guard.
    with MemtomemHybridStore(path, confirm_project_shared=True) as store:
        store.put(("docs",), "key", {"text": "apple"})
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE items SET namespace=?, key=?", (json.dumps([TOKEN]), TOKEN))
    with MemtomemHybridStore(path, confirm_project_shared=True) as store:
        assert store.get((TOKEN,), TOKEN)
        store.delete((TOKEN,), TOKEN)
        assert not store.export_json()["records"]
        assert all(not database_state(path)[table] for table in ("items", "item_fts", "vectors"))


# Literal identifiers from the existing HybridStore tests/guide and consumers,
# plus ordinary secret-label names and path/Unicode boundary cases. These are
# calibration fixtures, not evidence of a production false-positive rate.
NORMAL_IDENTIFIERS = [
    "docs",
    "a",
    "x",
    "same",
    "key",
    "hidden",
    "new",
    "graph",
    "slow",
    "test",
    "text",
    "extra",
    "different",
    "deterministic-v1",
    "users",
    "alice",
    "preferences",
    "/notes.md",
    "user",
    "memories",
    "agent",
    "filesystem",
    "api_key",
    "password",
    "access_token",
    "metadata.title",
    "authors[0].name",
    "context[*].content",
    "revisions[-1].changes",
    "$",
    "toy-python-v1",
    "사용자_기억",
    "550e8400-e29b-41d4-a716-446655440000",
    "tenant-42",
    "v1",
    "report1",
    "documents",
    "cache",
    "embeddings",
    "sections[*].paragraphs[*].text",
    "metadata.tags[*]",
    "passwd",
    "pwd",
    "secret_key",
]


@pytest.mark.parametrize("identifier", NORMAL_IDENTIFIERS)
def test_normal_identifiers_remain_usable(scoped_path, identifier):
    path = scoped_path["project_shared"]
    # Namespace periods are forbidden by LangGraph independently of privacy.
    namespace = ("docs", identifier) if "." not in identifier else ("docs",)
    with MemtomemHybridStore(
        path, confirm_project_shared=True, index={"fields": [identifier]}, index_id=identifier
    ) as store:
        store.put(namespace, identifier, {"text": "apple"}, index=[identifier])
        assert store.get(namespace, identifier).value == {"text": "apple"}


def test_scan_does_not_join_independent_labels_and_assignments(scoped_path):
    with MemtomemHybridStore(
        scoped_path["project_shared"],
        confirm_project_shared=True,
        index={"fields": ["password", "=example"]},
        index_id=":example",
    ) as store:
        store.put(("api_key",), "=example", {"text": "apple"}, index=["password", ":example"])
        assert store.get(("api_key",), "=example")


@pytest.mark.parametrize("index", [False, None, []])
@pytest.mark.parametrize("field", ["namespace", "key"])
def test_unindexed_writes_still_guard_identity(tmp_path, index, field):
    op = operation(field)
    with MemtomemHybridStore(tmp_path / "store.db") as store:
        with pytest.raises(ValueError, match="privacy"):
            store.put(op.namespace, op.key, op.value, index=index)
        assert store.export_json()["records"] == []


def test_configuration_outcome_is_separate_from_record_outcome(tmp_path):
    surface = "langgraph_hybridstore_init"
    before = privacy.snapshot()["by_tool"].get(surface, {})
    record_before = privacy.snapshot()["by_tool"].get("langgraph_hybridstore_put", {})
    with pytest.raises(ValueError, match="configuration blocked"):
        MemtomemHybridStore(tmp_path / "store.db", index_id=TOKEN)
    after = privacy.snapshot()["by_tool"][surface]
    assert after["blocked"] - before.get("blocked", 0) == 1
    assert after["pass"] == before.get("pass", 0)
    assert privacy.snapshot()["by_tool"].get("langgraph_hybridstore_put", {}) == record_before
