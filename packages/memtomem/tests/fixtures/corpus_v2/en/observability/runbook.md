> Synthetic content for search regression testing — verify before adopting as runbook.

## probe_http_status_code + blackbox.yml timeout tuning

<!-- primary: observability/synthetic -->
<!-- secondary: observability/alerting -->

To verify external endpoint health, check the `probe_http_status_code` metric in Prometheus. If the value is not 200, run `curl -v -L /healthz` from a node outside the cluster. Adjust the `timeout_seconds` in `blackbox.yml` if network latency causes false positives in the `ProbeFailed` alert.

## pg_stat_activity monitoring + promtool check rules

<!-- primary: observability/metrics -->
<!-- secondary: postgres/connection_pool -->

Monitor database connection saturation using `pg_stat_activity_count`. If connections exceed 90% of `max_connections`, increase the pool size in `pgbouncer.ini`. Run `promtool check rules recording_rules.yml` after updating any custom SQL exporter metrics to ensure recording consistency.

## amtool silence during mitigation phase

<!-- primary: observability/alerting -->
<!-- secondary: incident_response/mitigation -->

When a 'High Error Rate' alert triggers, acknowledge it in PagerDuty immediately. Use `amtool silence add alertname=HighErrorRate` to suppress duplicate notifications during the mitigation phase. Verify the fix by checking if `sum(rate(http_requests_total{status=~"5.."}[5m]))` returns to zero.

## OpenTelemetry SDK 1.24.0 + BatchSpanProcessor

<!-- primary: observability/tracing -->
<!-- secondary: api_design/grpc -->

Update the OpenTelemetry SDK version to 1.24.0 in your `pom.xml` to fix span leak issues. Ensure `SpanProcessor` is configured with a `BatchSpanProcessor` for performance. Test the propagation by verifying the presence of `X-B3-TraceId` headers in outgoing gRPC calls using a packet capture tool.
