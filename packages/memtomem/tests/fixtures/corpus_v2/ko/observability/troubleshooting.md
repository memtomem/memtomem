> Synthetic content for search regression testing — verify before adopting as runbook.

## topk 카디널리티 식별 + metric_relabel_configs drop

<!-- primary: observability/metrics -->
<!-- secondary: cost_optimization/observability -->

메트릭 저장소 용량이 급증하는 경우 `topk(10, count by (__name__) ({__name__=~".+"}))` 쿼리를 통해 카디널리티가 높은 메트릭을 식별하십시오. 특정 라벨에 `pod_name`과 같은 고유값이 포함되어 있을 가능성이 큽니다. `prometheus.yml`의 `metric_relabel_configs`에서 해당 라벨을 `drop` 처리하여 저장 비용을 최적화하십시오.

## Fluent-bit [out_fw] buffer full + Retry_Limit 제한

<!-- primary: observability/logging -->
<!-- secondary: -->

로그 유실 증상이 보이면 Fluent-bit 포드의 리소스를 체크하고 `kubectl logs` 명령어로 `[out_fw] buffer full` 에러 여부를 확인하십시오. 네트워크 지연으로 인해 Loki로의 전송이 실패했을 수 있습니다. `fluent-bit.conf` 내의 `Retry_Limit` 설정을 `False`에서 특정 횟수로 제한하여 무한 재시도로 인한 포드 재시작을 방지하십시오.

## traceparent 헤더 유실 + W3C Trace Context 통일

<!-- primary: observability/tracing -->
<!-- secondary: -->

서비스 간 트레이스 연결이 끊긴다면 HTTP 헤더에 `traceparent`가 제대로 포함되었는지 확인하십시오. `curl -v` 명령어로 응답 헤더를 출력하여 업스트림에서 전달된 Trace ID와 일치하는지 대조합니다. 만약 다를 경우 OpenTelemetry `Propagators` 설정이 `W3C Trace Context`로 통일되어 있는지 점검하십시오.

## Alertmanager group_interval 축소 + 알람 큐 점검

<!-- primary: observability/alerting -->
<!-- secondary: -->

알람 지연이 발생할 경우 Alertmanager 로그에서 `msg="Flushing alerts"` 시점과 실제 수신 시점을 비교하십시오. `alertmanager.yaml`의 `group_interval`이 너무 길게 설정되어 다수의 알람이 묶여있을 수 있습니다. 해당 값을 5분 이하로 줄이고 `amtool alert` 명령어를 통해 현재 대기 중인 알람 큐 상태를 확인하십시오.
