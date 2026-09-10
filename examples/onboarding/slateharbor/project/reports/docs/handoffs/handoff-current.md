---
tags: [slateharbor, reports, current]
---
# Slateharbor 리포트: 작업 인계

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## report freshness

작업: TASK-REPORT-19: 새 thread에서는 캐시 시각과 마지막 성공 배치부터 확인한다.

이미 확정: `report freshness` = `15`. 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: materialized_at과 원장 event watermark를 비교한다. 갱신 실패 시 이전 값과 지연 상태를 함께 보여준다.

보류 사항: 원인 확인 없이 이전 `60`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## CSV export rows

작업: TASK-REPORT-20: job 완료 알림에 재다운로드 링크를 넣는다.

이미 확정: `CSV export rows` = `10000`. 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: export 요청 ID로 기존 job을 조회하고 중복 클릭에는 같은 ID를 반환한다.

보류 사항: 원인 확인 없이 이전 `50000`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## report window

작업: TASK-REPORT-21: 기간 초과 안내에 비동기 export를 제안한다.

이미 확정: `report window` = `90`. 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 요청 기간을 확인하고 읽기 전용 분석 경로로 우회한다.

보류 사항: 원인 확인 없이 이전 `365`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## report cache ttl

작업: TASK-REPORT-22: 권한 축소 후 cache 접근 테스트를 추가한다.

이미 확정: `report cache ttl` = `300`. 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: cache key의 tenant와 permission revision을 확인하고 영향을 받은 key만 무효화한다.

보류 사항: 원인 확인 없이 이전 `1800`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## aggregate group privacy

작업: TASK-REPORT-23: 교차 필터로 작은 그룹이 드러나는지 검토한다.

이미 확정: `aggregate group privacy` = `5`. 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 그룹 크기를 확인하고 작은 그룹은 상위 집계로 합친다.

보류 사항: 원인 확인 없이 이전 `1`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## report query timeout

작업: TASK-REPORT-24: empty와 failed 상태의 화면 테스트를 추가한다.

이미 확정: `report query timeout` = `8`. 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: query 상태와 오류 코드를 확인하고 마지막 정상 결과와 구분해서 표시한다.

보류 사항: 원인 확인 없이 이전 `30`로 되돌리는 작업은 제안 단계로 남겨 둡니다.
