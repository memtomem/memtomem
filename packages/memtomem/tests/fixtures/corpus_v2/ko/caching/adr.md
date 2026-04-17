> Synthetic content for search regression testing — verify before adopting as runbook.

## Redis Cluster 대신 Master-Replica + Sentinel 채택

<!-- primary: caching/redis -->
<!-- secondary: caching/replication -->

Redis Cluster 대신 단일 Master-Replica 구성을 채택했다. 클러스터는
슬롯 관리 오버헤드가 크지만, Sentinel 방식은 운영이 단순하다는
장점이 있다. 노드 가용성 확보 대신 쓰기 확장성 제한이라는 trade-off
를 수용하기로 결정했다. 샤드당 `used_memory` 가 16GB 를 초과하거나
초당 커맨드 수가 50k 를 넘어서면 Cluster 전환을 재검토한다.

## TTL 만료 대신 Kafka 기반 명시적 캐시 무효화

<!-- primary: caching/invalidation -->
<!-- secondary: kafka/consumer, caching/redis -->

애플리케이션 내 TTL 만료 방식이 아닌 Kafka 기반의 명시적 캐시
무효화를 선택했다. TTL 방식은 구현이 쉽지만, DB 와의 정합성 지연
시간이 길어지는 단점이 있다. 메시지 큐 관리 비용을 감수하는 대신
정합성을 극대화하기로 결정했다. 컨슈머의 `lag` 지표가 지속적으로
1000개를 상회하여 정합성 보장이 어려워지는 상황에서 재평가한다.

## volatile-lru 대신 allkeys-lru eviction 정책

<!-- primary: caching/eviction -->
<!-- secondary: caching/redis, observability/metrics -->

기본값인 `volatile-lru` 대신 `allkeys-lru` 정책을 채택했다. 전자는
만료 시점이 설정된 키만 삭제하지만, 후자는 모든 키를 대상으로
메모리를 확보한다. 캐시 적중률 (Hit rate) 변동 위험은 존재하나
메모리 부족으로 인한 OOM 발생을 방지하기 위한 trade-off 로 결정했다.
`evicted_keys` 수치가 시간당 1M 을 돌파하여 성능 저하가 가시화되면
eviction 정책을 재검토한다.

## Fetch-and-Set 대신 Lua PER 알고리즘 (stampede 방어)

<!-- primary: caching/stampede -->
<!-- secondary: caching/redis, observability/metrics -->

단순 Fetch-and-Set 방식이 아닌 Redis 루아 스크립트를 이용한 PER
(Probabilistic Early Recomputation) 알고리즘을 선택했다. 캐시 만료
시점에 트래픽이 몰리는 Stampede 현상을 방지하기 위함이다. 코드
복잡도가 증가하는 반면 백엔드 DB 의 순간적인 부하 폭증을 차단할 수
있다고 판단했다. 서비스 트래픽이 현재 대비 300% 이상 증가하여
레이턴시 분산 효과가 미미해지면 재평가한다.
