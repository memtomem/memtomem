> Synthetic content for search regression testing — verify before adopting as runbook.

## Grafana + kubectl scale 로 파드 수동 확장

<!-- primary: observability/metrics -->
<!-- secondary: k8s/scaling -->

Grafana 대시보드에서 `rate(http_requests_total[1m])` 쿼리를 실행하여 현재 트래픽 변화를 확인하십시오. 만약 초당 요청 수가 500을 초과하면 `kubectl scale deployment/api --replicas=10` 명령어로 파드를 수동 확장합니다. 작업 후 `up` 메트릭이 1인지 확인하여 신규 파드의 스크레이프 상태를 점검하십시오.

## Fluent-bit Mem_Buf_Limit 상향 + DaemonSet 재시작

<!-- primary: observability/logging -->
<!-- secondary: -->

로그 수집 이슈가 발생하면 `kubectl logs -n logging fluent-bit-ds` 명령어로 에러 로그를 확인하십시오. `fluent-bit.conf` 파일에서 `Mem_Buf_Limit` 수치를 50MB로 상향 조정하여 버퍼 오버플로우를 방지합니다. 수정 후 `kubectl rollout restart ds/fluent-bit`을 실행하여 설정을 적용하십시오.

## otel-collector + Jaeger trace_id 유실 점검

<!-- primary: observability/tracing -->
<!-- secondary: observability/metrics -->

트레이스 유실 시 `otel-collector` 설정 파일의 `exporters: logging` 섹션을 활성화하여 로그를 확인하십시오. Jaeger UI에서 `trace_id` 검색이 되지 않는다면 애플리케이션의 `OTEL_EXPORTER_OTLP_ENDPOINT` 주소를 확인하십시오. 수집 서버에서 `otelcol_processor_batch_dropped_spans` 메트릭이 상승 중인지 체크하십시오.

## Alertmanager Slack 웹훅 변경 + amtool config check

<!-- primary: observability/alerting -->
<!-- secondary: -->

알람 수신 채널을 변경하려면 `alertmanager.yaml` 내의 `receiver` 섹션을 수정하십시오. 신규 Slack 웹훅 URL을 `slack_configs` 하단에 추가합니다. `amtool config check` 명령어로 문법 오류를 검증한 후, `curl -X POST http://alertmanager:9093/-/reload`를 호출하여 설정을 갱신하십시오.
