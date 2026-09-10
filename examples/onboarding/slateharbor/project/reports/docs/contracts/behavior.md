---
tags: [slateharbor, reports, current]
---
# Slateharbor 리포트: 동작 계약

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## report freshness

입력: `age_minutes`. `report freshness`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `freshness_minutes`입니다.

승인 예: `10`. 거절 예: `30`. 기준은 [운영 설정](../../config/production.json)의 `15`입니다.

업무 의미: 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다.

잘못된 적용 사례: INC-REPORT-08에서 어제 생성된 캐시가 최신 매출처럼 표시돼 운영자가 정산을 다시 요청했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## CSV export rows

입력: `rows`. `CSV export rows`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `export_rows`입니다.

승인 예: `5000`. 거절 예: `20000`. 기준은 [운영 설정](../../config/production.json)의 `10000`입니다.

업무 의미: 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다.

잘못된 적용 사례: 동기 요청 timeout 뒤 사용자가 다시 눌러 같은 export가 여러 번 만들어졌다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## report window

입력: `days`. `report window`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `window_days`입니다.

승인 예: `30`. 거절 예: `180`. 기준은 [운영 설정](../../config/production.json)의 `90`입니다.

업무 의미: 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다.

잘못된 적용 사례: 1년 범위의 조인 쿼리가 운영 DB의 온라인 트래픽을 밀어냈다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## report cache ttl

입력: `age_seconds`. `report cache ttl`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `cache_seconds`입니다.

승인 예: `100`. 거절 예: `600`. 기준은 [운영 설정](../../config/production.json)의 `300`입니다.

업무 의미: 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다.

잘못된 적용 사례: 권한 변경 뒤에도 오래된 cache가 보였고 조직 구분이 빠진 key가 발견됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## aggregate group privacy

입력: `members`. `aggregate group privacy`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `minimum_group`입니다.

승인 예: `8`. 거절 예: `2`. 기준은 [운영 설정](../../config/production.json)의 `5`입니다.

업무 의미: 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다.

잘못된 적용 사례: 한 명뿐인 팀의 통계가 개인 활동 내역처럼 표시됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## report query timeout

입력: `elapsed_seconds`. `report query timeout`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `query_seconds`입니다.

승인 예: `4`. 거절 예: `15`. 기준은 [운영 설정](../../config/production.json)의 `8`입니다.

업무 의미: 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다.

잘못된 적용 사례: 30초까지 실행되는 조회가 운영 DB 커넥션을 오래 점유해 같은 시각의 다른 리포트 요청까지 함께 느려졌다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.
