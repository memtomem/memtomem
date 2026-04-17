> Synthetic content for search regression testing — verify before adopting as runbook.

## WAF rule expiration caused probe 403 false positive

<!-- primary: observability/synthetic -->
<!-- secondary: networking/load_balancing -->

On 2026-04-15, a false positive 'Service Down' alert was triggered by a misconfigured synthetic check. The `probe_http_status_code` returned 403 due to an expired WAF rule blocking the prober IP. We updated the `blackbox_exporter` white-list and added `probe_duration_seconds` monitoring to detect network latency before alerts fire.

## user_id label explosion caused cloud bill spike

<!-- primary: observability/metrics -->
<!-- secondary: cost_optimization/observability -->

A significant cloud bill increase in March was traced back to high-cardinality metrics. A new deployment introduced a `user_id` label in `http_server_requests_seconds_count`, creating millions of series. We remediated this by applying `drop` rules in Prometheus and implemented a strict metric review policy for new labels.

## otel-collector OOM from frontend span volume

<!-- primary: observability/tracing -->
<!-- secondary: -->

Investigating a latency spike on 2026-04-02 revealed that the `otel-collector` was OOMing. High span volume from the frontend service exceeded the default `memory_limiter` threshold. We increased the memory limit to 2Gi and tuned the `send_batch_size` to 1000 to stabilize the pipeline during peak traffic hours.

## alert fatigue from loose repeat_interval

<!-- primary: observability/alerting -->
<!-- secondary: incident_response/oncall -->

The incident on 2026-03-12 lasted longer than expected due to 'alert fatigue'. Critical signals were buried under 200+ warnings because of a loose `repeat_interval` in `alertmanager.yaml`. We consolidated alert rules using `sum by (cluster)` and implemented a hierarchical notification status system to prioritize urgent system failures.
