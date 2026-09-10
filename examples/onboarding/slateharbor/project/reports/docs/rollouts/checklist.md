---
tags: [slateharbor, reports, current]
---
# Slateharbor 리포트: 단계 배포 점검

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## report freshness

변경 대상: `report freshness`의 `freshness_minutes`. 이전 `60`, 현재 `15`.

배포 전: materialized_at과 원장 event watermark를 비교한다. 갱신 실패 시 이전 값과 지연 상태를 함께 보여준다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: INC-REPORT-08에서 어제 생성된 캐시가 최신 매출처럼 표시돼 운영자가 정산을 다시 요청했다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다.

배포 후 담당 작업: TASK-REPORT-19: 새 thread에서는 캐시 시각과 마지막 성공 배치부터 확인한다.

## CSV export rows

변경 대상: `CSV export rows`의 `export_rows`. 이전 `50000`, 현재 `10000`.

배포 전: export 요청 ID로 기존 job을 조회하고 중복 클릭에는 같은 ID를 반환한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 동기 요청 timeout 뒤 사용자가 다시 눌러 같은 export가 여러 번 만들어졌다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다.

배포 후 담당 작업: TASK-REPORT-20: job 완료 알림에 재다운로드 링크를 넣는다.

## report window

변경 대상: `report window`의 `window_days`. 이전 `365`, 현재 `90`.

배포 전: 요청 기간을 확인하고 읽기 전용 분석 경로로 우회한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 1년 범위의 조인 쿼리가 운영 DB의 온라인 트래픽을 밀어냈다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다.

배포 후 담당 작업: TASK-REPORT-21: 기간 초과 안내에 비동기 export를 제안한다.

## report cache ttl

변경 대상: `report cache ttl`의 `cache_seconds`. 이전 `1800`, 현재 `300`.

배포 전: cache key의 tenant와 permission revision을 확인하고 영향을 받은 key만 무효화한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 권한 변경 뒤에도 오래된 cache가 보였고 조직 구분이 빠진 key가 발견됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다.

배포 후 담당 작업: TASK-REPORT-22: 권한 축소 후 cache 접근 테스트를 추가한다.

## aggregate group privacy

변경 대상: `aggregate group privacy`의 `minimum_group`. 이전 `1`, 현재 `5`.

배포 전: 그룹 크기를 확인하고 작은 그룹은 상위 집계로 합친다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 한 명뿐인 팀의 통계가 개인 활동 내역처럼 표시됐다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다.

배포 후 담당 작업: TASK-REPORT-23: 교차 필터로 작은 그룹이 드러나는지 검토한다.

## report query timeout

변경 대상: `report query timeout`의 `query_seconds`. 이전 `30`, 현재 `8`.

배포 전: query 상태와 오류 코드를 확인하고 마지막 정상 결과와 구분해서 표시한다.

배포 중: staging 입력 검증 후 pilot tenant, 일반 tenant 순으로 관찰합니다.

중단 조건: 30초까지 실행되는 조회가 운영 DB 커넥션을 오래 점유해 같은 시각의 다른 리포트 요청까지 함께 느려졌다.

되돌림 판단: 값만 바꾸지 말고 [채택 이유](../decisions/decision-current.md)를 확인합니다. 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다.

배포 후 담당 작업: TASK-REPORT-24: empty와 failed 상태의 화면 테스트를 추가한다.
