# CPU ONNX profiles and evaluation

New ONNX installs use multilingual-e5-small FP32. Existing explicitly selected
BGE-M3 indexes retain their model and dimensions. See [the measured results and limitations](REPORT.ko.md). The first local comparison
favours E5 for latency/CPU, but BGE-M3 for retrieval quality. **No existing
production index has been migrated.** Quality acceptance remains the operator's
decision after reviewing results.

## Model contracts

| Profile | Dimensions | Input limit | Pooling / normalization | Roles |
|---|---:|---:|---|---|
| E5-small | 384 | 512, complete rendered input | mean / L2 | `query: ` and `passage: ` |
| BGE-M3 | 1024 | model 8192; existing configured cap retained | CLS / L2 | no E5 prefix |

E5 uses official revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`.
Its exact tokenizer and FP32 ONNX file are checksummed before use. Model,
dimension, revision, role/pooling contract, input cap and quantization manifest
participate in stored embedding identity. Quantized artifacts never silently
fall back to FP32 or another ISA profile. Existing BGE legacy input truncation
warnings remain; E5 rejects oversized direct inputs.

| Device | Initial choice | Optional evaluation candidates | Local validation |
|---|---|---|---|
| macOS ARM64 | E5 FP32, threads 2, batch 4, arena off | INT8 ARM64 | measured here |
| macOS Intel | FP32 | INT8 AVX2, subject to measurement | NOT_RUN |
| Windows x86-64 | FP32 | INT8 AVX2 / AVX512 / VNNI, selected for host | NOT_RUN |
| Linux x86-64 | FP32 | INT8 AVX2 / AVX512 / VNNI, selected for host | NOT_RUN |
| Windows / Linux ARM64 | FP32 where ORT wheels are available | INT8 ARM64 | NOT_RUN |

Variant names describe export recipes and target architecture, not a promise
that a CPU has a particular instruction set or will run faster. ORT dispatches
CPU kernels. FP16/O4 is not a CPU default; INT4 and an OpenVINO backend are out
of scope. The manually dispatched `CPU ONNX smoke` workflow performs real FP32
inference on macOS, Ubuntu and Windows; a workflow file is not a passing CI run.

## Reproduce

Use the project environment for inference. In an isolated export environment,
install `requirements-export.txt`; the benchmark also needs `psutil==7.2.2`.
Inference measurements used ORT 1.24.4, FastEmbed 0.8.0, NumPy 2.4.4 and
Tokenizers 0.22.2; export used ORT 1.23.2 / ONNX 1.19.1 / NumPy 2.5.3.
`uv run --frozen --with psutil==7.2.2 python ...` can supply psutil without
changing the project lock. Benchmark scripts reject imports from another checkout.
Set `MEMTOMEM_FASTEMBED_CACHE` to an evaluation cache, separate from production.
Download official weights before timing and use `HF_HUB_OFFLINE=1` during runs.

```
python tools/cpu-embedding/export_int8.py --source /cache/models--intfloat--multilingual-e5-small/snapshots/614241f622f53c4eeff9890bdc4f31cfecc418b3 --model intfloat/multilingual-e5-small --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3 --variant int8-arm64 --output /tmp/e5-int8
python tools/cpu-embedding/benchmark.py prepare --output /tmp/fixtures.json
python tools/cpu-embedding/benchmark.py run --corpus /tmp/fixtures.json --artifact /tmp/e5-int8 --output /tmp/comparison
python tools/cpu-embedding/summarize.py /tmp/comparison --output /tmp/summary.json
python tools/cpu-embedding/sweep.py --corpus /tmp/fixtures.json --artifact /tmp/e5-int8 --output /tmp/sweep
python tools/cpu-embedding/smoke.py
```

Export defaults to per-channel dynamic MatMul quantization. Large embedding
tables remain FP32, so this is **not** a claim of 4x smaller files. The separate
`--quantize-embeddings` candidate also quantizes Gather and needs its own quality
measurement. Manifests include source hash, revision, recipe, tool versions and
all artifact file hashes. FP32 is the universal starting choice, not an implicit
quality claim about a quantized candidate.

The fixture corpus has 192 bilingual documents and 120 frozen queries. All
matched inputs fit both models' 512-token comparison limit; this is dense-only
retrieval, not the production hybrid pipeline. Qrels precede inference. Runs
use independent processes, alternate model order, and report Recall/MRR/nDCG@10,
zero-hit rate, CPU seconds, query p50/p95, sampled peak CPU/RSS and retained RSS.
Document vector cosine and top-10 overlap measure quantization drift only within
the same model. Downloads/export are excluded; cold load is reported separately.
Desktop background work and allocator behaviour can affect resource measurements.

`prepare_miracl.py` selects 200 dev queries per language by stable ID hash,
requires every judged passage, adds 1000 deterministic distractors per language,
and freezes hashes before inference. Long passages are losslessly split; ranks
collapse fragments by original document ID. This is an explicit judged-pool
subset, never an official full MIRACL score. Public API retrieval encountered
HTTP 404/500. The measured fallback used MTEB's official candidate-pool copies,
verified queries against original dev TSVs, and required all positive qrels.
`--positive-qrels-only` explicitly records omitted negative judgments (167 en,
330 ko), rather than silently weakening the default completeness check.
Distractors come from the MTEB candidate pool, not all Wikipedia. Monolingual
ranking uses `--monolingual`. Document vectors can be reused with corpus/model/
policy checks, avoiding repeated corpus encoding when checking query ranking.
The formal public quality comparison is one run per model, not three full
corpus encodes; the independent CPU/thread/batch matrix has 81 runs.

## Indexing, safety and migration review

`prepare_clone.py --database ... --config ... --source ... --output ...` backs
up SQLite using its read-only backup API and copies selected source bytes into
private evaluation storage. It does not rewrite production sources/configuration.
`index_clone.py` opens a fresh per-run DB and measures initial indexing plus
unchanged reinspection. `watchers.py` drives one or three independent real
watcher queue consumers using synthetic notifications against a shared clone;
native filesystem delivery is a separate check.

The clone is an evaluation subset, **not** a drop-in production migration.
Before a production switch, review all sources (including privacy-blocked and
missing sources), preserve namespace/tags/scope/validity/origin via the existing
bounded migration machinery, regenerate vectors into the new dimension, and
verify original-source span provenance. Keep the original configuration, SQLite
backup and original model artifacts together for rollback. Do not point an old
1024-dimensional database at E5 or reset/restart shared sessions implicitly.

Completed-source receipts are recorded inside the chunk transaction and reused
under the existing cross-process source lock, after privacy and namespace checks.
They bind source generation, configuration, tokenizer and stored chunk/index
state. Force/reassignment, enrichment and automatic summaries take the normal
path. Metadata changes and missing vectors invalidate reuse; receipts never
pretend an incomplete vector index was repaired. CPU logs expose read, lock,
chunk, embedding and database phases without source text. Quantized E5 uses the
artifact's checksummed tokenizer, so a separate Hub cache is unnecessary offline.

## Chunk-size audit

| Surface | Enforcement |
|---|---|
| Markdown, merged headings, bullets, tables, fences | common `IndexEngine.chunk_content` final `bound_chunks` |
| JSON and source code, including parser fallback | bounded structured/code chunkers preserve original slices |
| Soft target/min/max and overlap | packing first, final exact body/composed ceiling after transforms |
| Retrieval titles, context and enrichment | description budget plus complete rendered input count |
| SQLite writes and import | configured budget validates final retrieval content before persistence |
| MCP/web edits and source rewrites | existing source-read-only and original-span provenance checks retained |
| Direct E5 embedding / Store callbacks using OnnxEmbedder | final prefixed input overflow refused before inference |
| Store with caller-supplied third-party embeddings | caller's model contract; no automatic claim of E5 limits |
| Audit/migration | canonical effective E5 defaults and pinned tokenizer; production apply requires review |

References: [E5 model card](https://huggingface.co/intfloat/multilingual-e5-small),
[Sentence Transformers efficiency](https://www.sbert.net/docs/sentence_transformer/usage/efficiency.html),
[ORT quantization](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html),
[ORT float16](https://onnxruntime.ai/docs/performance/model-optimizations/float16.html),
[MIRACL](https://github.com/project-miracl/miracl).

`prepare_replay.py` freezes 100 real historical queries for unjudged latency and
ranking-change replay. The production snapshot had only one independent label;
past result order is never promoted to qrels. Its 20 mixed-script queries are
not claimed to be 20 judged cross-language cases. `--reuse-after-first` encodes
the common corpus once and repeats queries in independent processes; summarize
encoding and query-only resource runs separately. Private text and raw query
history stay outside the repository; committed summaries contain only aggregates.

The MIRACL fallback sources were [MTEB English](https://huggingface.co/datasets/mteb/MIRACLRetrieval_en_top_250_only_w_correct-v2) and [MTEB Korean](https://huggingface.co/datasets/mteb/MIRACLRetrieval_ko_top_250_only_w_correct-v2), cross-checked against the original MIRACL dev TSVs. See `miracl-provenance.json` for frozen hashes and sampling scope.
