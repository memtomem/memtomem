> Synthetic content for search regression testing — verify before adopting as runbook.

## 2026-02-14 noeviction 설정으로 인한 주문 API 레이턴시 급등

<!-- primary: caching/eviction -->
<!-- secondary: caching/redis, observability/metrics -->

2026-02-14 10:30 KST 경 주문 서비스의 레이턴시가 평소보다 5배
증가했다. 원인은 Redis 의 `maxmemory-policy` 가 `noeviction` 으로
설정되어 메모리 풀 상태에서 신규 캐시 쓰기가 거부된 것이었다.
`evicted_keys` 메트릭이 0으로 고정된 것을 확인한 후 정책을
`allkeys-lru` 로 수정하여 해결했다. 후속 조치로 Prometheus 알람
임계치에 메모리 사용률 90% 를 추가했다.

## 2026-03-21 트랜잭션 격리로 인한 프로필 업데이트 미반영

<!-- primary: caching/invalidation -->
<!-- secondary: postgres/replication, api_design/idempotency -->

2026-03-21 15:45 KST 에 사용자 프로필 업데이트가 반영되지 않는
현상이 보고됐다. 조사 결과 DB Write 이후 `cache-invalidator` 워커에서
트랜잭션 격리 수준 문제로 무효화 메시지를 누락한 것이 근본
원인이었다. `wal_level` 로그를 분석하여 유실된 시퀀스를 확인하고
캐시를 수동으로 제거하여 정합성을 복구했다. 후속 조치로 CDC 기반의
무효화 로직을 도입하여 안정성을 강화했다.

## 2026-04-02 메인 배너 TTL 동기 만료로 인한 stampede

<!-- primary: caching/stampede -->
<!-- secondary: observability/metrics, incident_response/mitigation -->

2026-04-02 09:00 KST, 메인 배너 이미지의 TTL 이 만료되면서 캐시 쇄도
현상이 발생했다. 캐시 미스로 인해 백엔드 DB 에 `IOPS` 임계치가 넘는
쿼리가 집중되며 서비스가 일시 마비됐다. 원인은 지터 (Jitter) 없이
고정된 만료 시간 설정으로 확인되어 확률적 만료 알고리즘을 적용했다.
후속 조치로 분산 락 (Distributed Lock) 라이브러리를 업데이트하고
중복 쿼리 방지 로직을 추가했다.

## 2026-05-12 AZ 단절로 인한 Redis 복제 지연

<!-- primary: caching/replication -->
<!-- secondary: networking/load_balancing, k8s/scheduling -->

2026-05-12 23:10 KST, 특정 가용 영역 (AZ) 의 네트워크 단절로 인해
Redis 복제 지연이 발생했다. `master_link_status` 가 down 으로 변하며
읽기 전용 복제본에서 오래된 데이터를 반환하는 현상이 확인됐다.
복제본의 `slave-read-only` 설정을 검토하고 수동으로 Failover 를
수행하여 마스터 노드를 재선출했다. 후속 조치로 k8s
`PodAntiAffinity` 설정을 강화하여 Redis 파드의 물리적 배분을
최적화했다.
