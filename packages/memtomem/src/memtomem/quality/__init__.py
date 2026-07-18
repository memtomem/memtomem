"""Memory Quality Lab (#1798): local search-evaluation building blocks.

This package holds the runtime-importable pieces of the Quality Lab, in four
modules:

- :mod:`memtomem.quality.metrics` — pure-function IR metrics (hit rate, MRR,
  recall, NDCG, precision).
- :mod:`memtomem.quality.fingerprints` — profile / corpus / index / case-set
  fingerprints that give a replay report its drift and identity context.
- :mod:`memtomem.quality.state` — assembles the live fingerprints from storage
  and classifies a profile's replay determinism.
- :mod:`memtomem.quality.replay` — replays stored eval cases into a
  deterministic report; :mod:`memtomem.quality.compare` diffs two such reports
  (report-to-report, no storage needed).

The CLI surface for all of this is ``mm quality`` (:mod:`memtomem.cli.quality_cmd`).
"""
