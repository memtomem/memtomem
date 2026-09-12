---
tags: [slateharbor, reports, current]
---
# Slateharbor 리포트: 복구 절차

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## report freshness

시작 조건: INC-REPORT-08에서 어제 생성된 캐시가 최신 매출처럼 표시돼 운영자가 정산을 다시 요청했다.

1. materialized_at과 원장 event watermark를 비교한다. 갱신 실패 시 이전 값과 지연 상태를 함께 보여준다.

2. [운영 설정](../../config/production.json)에서 `report freshness` / `freshness_minutes`의 현재 값 `15`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다.

## CSV export rows

시작 조건: 동기 요청 timeout 뒤 사용자가 다시 눌러 같은 export가 여러 번 만들어졌다.

1. export 요청 ID로 기존 job을 조회하고 중복 클릭에는 같은 ID를 반환한다.

2. [운영 설정](../../config/production.json)에서 `CSV export rows` / `export_rows`의 현재 값 `10000`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다.

## report window

시작 조건: 1년 범위의 조인 쿼리가 운영 DB의 온라인 트래픽을 밀어냈다.

1. 요청 기간을 확인하고 읽기 전용 분석 경로로 우회한다.

2. [운영 설정](../../config/production.json)에서 `report window` / `window_days`의 현재 값 `90`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다.

## report cache ttl

시작 조건: 권한 변경 뒤에도 오래된 cache가 보였고 조직 구분이 빠진 key가 발견됐다.

1. cache key의 tenant와 permission revision을 확인하고 영향을 받은 key만 무효화한다.

2. [운영 설정](../../config/production.json)에서 `report cache ttl` / `cache_seconds`의 현재 값 `300`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다.

## aggregate group privacy

시작 조건: 한 명뿐인 팀의 통계가 개인 활동 내역처럼 표시됐다.

1. 그룹 크기를 확인하고 작은 그룹은 상위 집계로 합친다.

2. [운영 설정](../../config/production.json)에서 `aggregate group privacy` / `minimum_group`의 현재 값 `5`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다.

## report query timeout

시작 조건: 30초까지 실행되는 조회가 운영 DB 커넥션을 오래 점유해 같은 시각의 다른 리포트 요청까지 함께 느려졌다.

1. query 상태와 오류 코드를 확인하고 마지막 정상 결과와 구분해서 표시한다.

2. [운영 설정](../../config/production.json)에서 `report query timeout` / `query_seconds`의 현재 값 `8`를 확인합니다. staging 값과 섞지 않습니다.

3. [구현](../../src/policy.py)에 같은 정책이 적용되는지 accepted/rejected 입력으로 확인합니다.

종료 조건: 영향받은 요청의 결과와 현재 설정이 일치해야 합니다. 관찰 불가를 정상으로 표시하지 않습니다. 이유: 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다.
