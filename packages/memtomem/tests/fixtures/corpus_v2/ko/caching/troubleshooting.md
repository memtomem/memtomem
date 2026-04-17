> Synthetic content for search regression testing — verify before adopting as runbook.

## 간헐적 캐시 미스 · eviction 폭증 진단

<!-- primary: caching/redis -->
<!-- secondary: caching/eviction, observability/metrics -->

애플리케이션에서 특정 키에 대한 GET 요청 시 간헐적으로 `(nil)`이 반환
되거나 응답 속도가 튀는 현상이 발생하면 먼저 Redis의 `evicted_keys`
지표를 확인하세요. 만약 `maxmemory`에 도달한 상태에서 `allkeys-lru`
정책이 적용 중이라면, 빈번하게 액세스되는 데이터가 메모리 부족으로
인해 강제 삭제되고 있을 가능성이 높습니다.

이 경우 `INFO stats` 명령어로 `evicted_keys` 수치가 급증했는지
점검하고, `MEMORY USAGE <key>`를 통해 특정 데이터가 과도하게 공간을
점유하는지 확인해야 합니다. 임시 방안으로는 중요도가 낮은 데이터의
TTL을 짧게 조정하여 메모리 여유 공간을 확보하세요. 근본적으로는
인스턴스 스케일업이나 클러스터 노드 확장이 필요합니다.

## RedisConnectionException — TCP 커넥션 풀 고갈

<!-- primary: caching/redis -->
<!-- secondary: networking/connection_pool, observability/metrics -->

클라이언트 라이브러리에서 `RedisConnectionException`이 발생하며
'연결을 사용할 수 없음' 오류가 반복된다면, TCP 커넥션 풀 고갈을
의심해야 합니다. 특히 커넥션 생성 시 `SYN_SENT` 상태에서 타임아웃이
발생한다면 서버측 `tcp-backlog` 설정값과 OS의 `somaxconn` 수치를 대조해
보세요.

증상 재현을 위해 `netstat -an | grep 6379 | wc -l`로 현재 커넥션 수를
체크하고, 소스 코드 내에서 `Jedis`나 `Lettuce` 객체가
`try-with-resources` 구문 밖에서 생성되어 반환되지 않고 있는지 검토가
필요합니다. 만약 서버 CPU 부하가 높지 않은데도 연결만 실패한다면
클라이언트측의 `max-active` 설정을 현재 트래픽 규모에 맞게 상향
조정하여 해결할 수 있습니다.

## 레이턴시 스파이크 — SLOWLOG · THP 점검

<!-- primary: caching/redis -->
<!-- secondary: observability/metrics -->

Redis 응답 시간이 갑자기 수백 밀리초 단위로 튀는 현상이 발생한다면,
먼저 `SLOWLOG GET 128` 명령을 통해 실행 시간이 긴 커맨드가 있는지
확인해야 합니다. 특히 대규모 컬렉션에 대해 `KEYS *`나 `HGETALL`,
`SMEMBERS`와 같은 O(N) 연산이 수행되고 있다면 싱글 스레드 구조상 전체
요청에 병목이 발생하게 됩니다.

만약 슬로우 로그에 잡히는 항목이 없다면 `redis-cli --intrinsic-latency
100`을 실행하여 OS 레벨의 지연 시간이 있는지 점검하세요. 투명한 거대
페이지(Transparent Huge Pages, THP) 설정이 활성화되어 있으면 스냅샷
생성 시 메모리 할당 지연으로 인해 레이턴시 스파이크가 생길 수 있으므로,
`/sys/kernel/mm/transparent_hugepage/enabled` 값을 `never`로 설정하여
해결되는지 확인이 필요합니다.

## Cache stampede 증상과 방어

<!-- primary: caching/stampede -->
<!-- secondary: caching/eviction, caching/invalidation -->

특정 시간에 캐시 히트율(Hit Rate)이 급격히 떨어지며 DB 부하가 치솟는
다면 '캐시 스탬피드(Cache Stampede)' 현상을 의심해야 합니다. 이는 만료
시간이 동일하게 설정된 다수의 키가 동시에 삭제되면서, 수많은 리퀘스트
가 한꺼번에 원본 DB로 몰려 발생하는 전형적인 증상입니다.

서버 로그에서 동일한 DB 쿼리가 찰나의 순간에 수백 번 중복 실행되었는지
파악하십시오. 이를 방지하기 위한 워크아라운드로 각 캐시 키의 TTL에
`random.nextInt(300)` 같은 지터(Jitter)를 추가하여 만료 시점을 분산
시켜야 합니다. 이미 장애 상황이라면 애플리케이션 레벨에서 '확률적 조기
재설정(Probabilistic Early Recomputation)' 로직을 도입하거나, 세마포어
를 이용해 단 하나의 스레드만 캐시를 갱신하도록 잠금 처리를 하여 DB를
보호해야 합니다.
