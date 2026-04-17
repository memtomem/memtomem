> Synthetic content for search regression testing — verify before adopting as runbook.

## 라벨 오타로 severity:critical 알람 누락

<!-- primary: observability/alerting -->
<!-- secondary: incident_response/detection -->

2026-04-10 14:00 UTC경, 알람 설정 오류로 인해 결제 모듈 장애를 조기에 인지하지 못했습니다. `alertmanager.yaml`에서 라벨 필터링 중 오타가 발생하여 `severity: critical` 알람이 누락된 것이 원인이었습니다. 재발 방지를 위해 `amtool config check`를 CI 단계에 통합하고 알람 라우팅 테스트 케이스를 추가했습니다.

## tsdb_wal_segment_size 이슈로 메트릭 수집 중단

<!-- primary: observability/metrics -->
<!-- secondary: k8s/scaling -->

2026-03-25, 프로메테우스의 `tsdb_wal_segment_size` 이슈로 메트릭 수집이 중단되어 오토스케일링이 작동하지 않았습니다. 이로 인해 트래픽 급증 시점에 서비스 지연이 발생했습니다. 스토리지 볼륨을 확장하고 `memory.limit_in_bytes` 설정을 최적화하여 수집 서버의 안정성을 확보했습니다.

## log_level:debug 잘못 배포로 로그량 10배 증가

<!-- primary: observability/logging -->
<!-- secondary: observability/alerting -->

지난주 발생한 로그 파이프라인 지연은 특정 서비스의 디버그 로그 폭증이 원인이었습니다. `log_level: debug` 설정이 프로덕션에 잘못 배포되어 초당 로그량이 10배 증가했습니다. 현재는 `fluent-bit`에 `throttle` 필터를 추가하여 로그 유입량을 제한하고 비정상 수치를 감시하는 알람을 구축했습니다.

## B3-Propagation 누락으로 gRPC 트레이스 유실

<!-- primary: observability/tracing -->
<!-- secondary: api_design/grpc -->

최근 gRPC 타임아웃 장애 분석 과정에서 서비스 간 호출 추적이 불가능함을 발견했습니다. OpenTelemetry SDK의 `B3-Propagation` 설정 누락으로 Trace ID가 전파되지 않았습니다. 모든 마이크로서비스의 공통 라이브러리를 업데이트하고 `X-B3-TraceId` 헤더가 모든 요청에 포함되도록 강제했습니다.
