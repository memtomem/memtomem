---
tags: [slateharbor, reports, proposed]
---
# Slateharbor 리포트: 검토 중 제안

> 합성 프로젝트 기록. 상태: proposed. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## report freshness

상태: proposed. `report freshness` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: INC-REPORT-08에서 어제 생성된 캐시가 최신 매출처럼 표시돼 운영자가 정산을 다시 요청했다.

제안: TASK-REPORT-19: 새 thread에서는 캐시 시각과 마지막 성공 배치부터 확인한다.

현재 계약: `freshness_minutes` = `15`. 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## CSV export rows

상태: proposed. `CSV export rows` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 동기 요청 timeout 뒤 사용자가 다시 눌러 같은 export가 여러 번 만들어졌다.

제안: TASK-REPORT-20: job 완료 알림에 재다운로드 링크를 넣는다.

현재 계약: `export_rows` = `10000`. 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## report window

상태: proposed. `report window` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 1년 범위의 조인 쿼리가 운영 DB의 온라인 트래픽을 밀어냈다.

제안: TASK-REPORT-21: 기간 초과 안내에 비동기 export를 제안한다.

현재 계약: `window_days` = `90`. 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## report cache ttl

상태: proposed. `report cache ttl` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 권한 변경 뒤에도 오래된 cache가 보였고 조직 구분이 빠진 key가 발견됐다.

제안: TASK-REPORT-22: 권한 축소 후 cache 접근 테스트를 추가한다.

현재 계약: `cache_seconds` = `300`. 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## aggregate group privacy

상태: proposed. `aggregate group privacy` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 한 명뿐인 팀의 통계가 개인 활동 내역처럼 표시됐다.

제안: TASK-REPORT-23: 교차 필터로 작은 그룹이 드러나는지 검토한다.

현재 계약: `minimum_group` = `5`. 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## report query timeout

상태: proposed. `report query timeout` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 30초까지 실행되는 조회가 운영 DB 커넥션을 오래 점유해 같은 시각의 다른 리포트 요청까지 함께 느려졌다.

제안: TASK-REPORT-24: empty와 failed 상태의 화면 테스트를 추가한다.

현재 계약: `query_seconds` = `8`. 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.
