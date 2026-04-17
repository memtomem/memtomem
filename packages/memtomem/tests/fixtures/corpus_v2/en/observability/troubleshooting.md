> Synthetic content for search regression testing — verify before adopting as runbook.

## probe_success flapping + blackbox dns_config timeout

<!-- primary: observability/synthetic -->
<!-- secondary: networking/dns -->

If the `probe_success` metric is flapping, check for DNS resolution issues within the cluster. Run `kubectl exec` into the blackbox-exporter pod and test with `nslookup`. If resolution is slow, increase the `dns_config` timeout in the `blackbox.yml` file to avoid false negative synthetic alerts.

## Prometheus targets DOWN + kubernetes_sd_configs check

<!-- primary: observability/metrics -->
<!-- secondary: -->

Targets showing as 'DOWN' in Prometheus usually indicate a scrape config mismatch. Run `up == 0` to list failing endpoints. Check the `kubernetes_sd_configs` and verify if the `prometheus.io/scrape` annotation is set to 'true' on the target pods. Ensure the service port matches the `containerPort` in the spec.

## [PARSER] invalid time format + Time_Format key update

<!-- primary: observability/logging -->
<!-- secondary: security/access_control -->

Logs not appearing in the dashboard might be due to a timestamp parsing error. Search the collector logs for `[PARSER] invalid time format`. If the app uses a custom format, update the `Time_Format` key in your parser config. Verify permissions of the `/var/log/containers` directory if the collector cannot read source files.

## OTLP exporter timeout + send_batch_size + probabilistic_sampler

<!-- primary: observability/tracing -->
<!-- secondary: observability/metrics -->

High span drop rates are often caused by OTLP exporter timeouts. Monitor `otelcol_exporter_sent_spans` vs `otelcol_exporter_send_failed_spans`. Increase the `send_batch_size` in the batch processor config. If the backend is overwhelmed, consider increasing the sampling ratio using the `probabilistic_sampler` processor.
