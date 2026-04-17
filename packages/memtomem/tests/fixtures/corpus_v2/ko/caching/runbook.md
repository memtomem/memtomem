> Synthetic content for search regression testing — verify before adopting as runbook.

## Redis 메모리 임계치 도달 시 정책 점검

<!-- primary: caching/redis -->
<!-- secondary: caching/eviction, observability/metrics -->

Redis 클러스터의 메모리 임계치 도달 시 다음 절차를 수행한다.
`redis-cli` 로 접속하여 `CONFIG GET maxmemory-policy` 를 실행해 현재
정책이 `allkeys-lru` 인지 확인한다. `INFO memory` 명령어를 통해
`used_memory_peak_human` 지표를 점검하고 필요시
`CONFIG SET maxmemory 4gb` 로 상한을 확장한다.

## 세션 캐시 패턴 무효화

<!-- primary: caching/invalidation -->
<!-- secondary: caching/redis, incident_response/mitigation -->

데이터 부정합 발생 시 특정 패턴의 캐시를 즉시 제거한다.
`redis-cli --scan --pattern 'user:session:*'` 명령을 사용하여 무효화
대상 키 목록을 추출한다. 추출된 키들을 `DEL` 또는 `UNLINK` 명령어로
삭제하여 데이터 일관성을 확보한다. 작업 완료 후 애플리케이션 로그에서
`cache_miss` 발생 빈도가 정상 범위인지 모니터링한다.

## Cache stampede 감지 시 보호 로직 활성화

<!-- primary: caching/stampede -->
<!-- secondary: observability/metrics, api_design/rate_limiting -->

캐시 쇄도 (Stampede) 현상 감지 시 보호 로직을 활성화한다. Grafana
대시보드에서 `backend_service_latency` 가 급증하는지 확인하고 Redis 에
`SET key value EX 300 NX` 옵션을 적용해 분산 락을 설정한다. 트래픽
과부하를 방지하기 위해 `istio_requests_total` 지표를 참고하여 임시로
API Rate Limit 수치를 하향 조정한다.

## Redis 복제본 지연 대응 Failover

<!-- primary: caching/replication -->
<!-- secondary: incident_response/mitigation, networking/load_balancing -->

Redis 복제본 지연 (Replication Lag) 발생 시 마스터 노드 상태를
점검한다. `ROLE` 명령어로 현재 노드가 slave 인지 확인한 후
`INFO replication` 을 실행해 `master_link_down_since_seconds` 값을
체크한다. 지연 시간이 60초를 초과할 경우 `SLAVEOF NO ONE` 을 실행하여
해당 노드를 단독 마스터로 승격시키고 로드밸런서 타겟 그룹을 갱신한다.
