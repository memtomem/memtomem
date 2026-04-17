> Synthetic content for search regression testing — verify before adopting as runbook.

## Blackbox Exporter over RUM for external availability

<!-- primary: observability/synthetic -->
<!-- secondary: observability/metrics -->

We chose Prometheus Blackbox Exporter over third-party RUM tools for external availability monitoring. The `probe_success` metric will be the primary indicator for our global health dashboard. This allows us to maintain all telemetry within our internal Kubernetes cluster using the `prometheus-community/prometheus-blackbox-exporter` Helm chart.

## histogram_quantile over mean for HPA tail latency

<!-- primary: observability/metrics -->
<!-- secondary: k8s/scaling -->

The team decided to use Prometheus `histogram_quantile` over simple averages for HPA scaling decisions. We accepted the increased storage overhead in `prometheus_tsdb_head_series` to gain better p99 latency visibility. This ensures that scaling triggers respond to tail latency spikes rather than being masked by mean values.

## Loki over Elasticsearch for log storage cost

<!-- primary: observability/logging -->
<!-- secondary: cost_optimization/observability -->

We are migrating to Loki from Elasticsearch to optimize log storage costs. By using the `loki.source.file` component, we leverage the same labeling schema as our metrics. The trade-off is slower full-text search compared to Lucene, but we gain significant savings on the `chunk_target_size` optimization.

## Istio Envoy tracing over manual SDK instrumentation

<!-- primary: observability/tracing -->
<!-- secondary: networking/service_mesh -->

We selected Istio's native Envoy tracing integration instead of manual SDK instrumentation for initial coverage. We enabled `meshConfig.enableTracing: true` to automatically inject headers. This provides immediate spans for service-to-service communication without modifying application code, despite the lack of deep internal function visibility.
