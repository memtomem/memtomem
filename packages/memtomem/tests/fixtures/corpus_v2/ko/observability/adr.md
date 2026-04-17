> Synthetic content for search regression testing — verify before adopting as runbook.

## Datadog 대신 Prometheus + Grafana 도입

<!-- primary: observability/metrics -->
<!-- secondary: cost_optimization/observability -->

상용 솔루션의 높은 비용 문제를 해결하기 위해 Datadog 대신 Prometheus와 Grafana 조합을 선택했습니다. `scrape_interval`을 30초로 조정하여 스토리지 부하를 줄이기로 결정했습니다. 특정 커스텀 메트릭인 `http_requests_total`의 cardinality 이슈는 수용 가능한 수준으로 판단했습니다.

## Logstash 대신 Fluent-bit 사이드카 배포

<!-- primary: observability/logging -->
<!-- secondary: observability/metrics -->

비정형 로그 처리를 위해 Logstash 대신 Fluent-bit을 사이드카로 배포하기로 결정했습니다. `fluent-bit.conf` 내에서 `Parser` 설정을 통해 로그를 구조화함으로써 쿼리 성능을 확보하고자 합니다. 이 과정에서 발생하는 CPU 리소스 오버헤드는 `node_cpu_seconds_total` 메트릭으로 모니터링할 예정입니다.

## OpenTelemetry + Jaeger 분산 트레이싱 표준

<!-- primary: observability/tracing -->
<!-- secondary: api_design/grpc -->

분산 트레이싱 표준화를 위해 OpenTelemetry를 전면 도입하고 Jaeger를 백엔드로 사용하기로 했습니다. gRPC 호출 시 `traceparent` 헤더를 통해 컨텍스트를 전파하여 서비스 간 가시성을 확보합니다. 샘플링 비율은 `OTEL_TRACES_SAMPLER`를 통해 10%로 제한하여 네트워크 대역폭 낭비를 방지합니다.

## Alertmanager group_wait / group_interval 로 알람 피로 감소

<!-- primary: observability/alerting -->
<!-- secondary: incident_response/detection -->

알람 피로도를 줄이기 위해 단순 임계치 방식 대신 `alertmanager.yaml`에서 `group_wait`와 `group_interval`을 적극 활용하기로 했습니다. 모든 알람에는 `severity: critical` 레이블을 필수로 지정하여 PagerDuty 라우팅을 최적화합니다. 이는 장애 인지 시점의 노이즈를 최소화하기 위한 전략적 선택입니다.
