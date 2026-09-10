---
tags: [slateharbor, reports, current]
---
# Slateharbor 리포트: 지원 진단

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## report freshness

문의 유형: `report freshness`가 예상과 다르게 동작합니다.

증상: INC-REPORT-08에서 어제 생성된 캐시가 최신 매출처럼 표시돼 운영자가 정산을 다시 요청했다.

확인 질문: 요청이 production인지 staging인지, `age_minutes`의 실제 값이 얼마인지 먼저 확인합니다.

진단: materialized_at과 원장 event watermark를 비교한다. 갱신 실패 시 이전 값과 지연 상태를 함께 보여준다.

비교 기준: [현행 설정](../../config/production.json)의 `freshness_minutes` = `15`.

설명할 이유: 리포트는 최대 15분 지연을 허용하며 기준 시각을 화면에 표시한다. 결제 원장의 실시간 진실로 쓰지 않는다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## CSV export rows

문의 유형: `CSV export rows`가 예상과 다르게 동작합니다.

증상: 동기 요청 timeout 뒤 사용자가 다시 눌러 같은 export가 여러 번 만들어졌다.

확인 질문: 요청이 production인지 staging인지, `rows`의 실제 값이 얼마인지 먼저 확인합니다.

진단: export 요청 ID로 기존 job을 조회하고 중복 클릭에는 같은 ID를 반환한다.

비교 기준: [현행 설정](../../config/production.json)의 `export_rows` = `10000`.

설명할 이유: 동기 CSV는 1만 행까지이고 큰 내보내기는 job ID를 반환한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## report window

문의 유형: `report window`가 예상과 다르게 동작합니다.

증상: 1년 범위의 조인 쿼리가 운영 DB의 온라인 트래픽을 밀어냈다.

확인 질문: 요청이 production인지 staging인지, `days`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 요청 기간을 확인하고 읽기 전용 분석 경로로 우회한다.

비교 기준: [현행 설정](../../config/production.json)의 `window_days` = `90`.

설명할 이유: 대화형 조회는 90일 창으로 제한한다. 장기 감사 자료는 별도 batch export로 제공한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## report cache ttl

문의 유형: `report cache ttl`가 예상과 다르게 동작합니다.

증상: 권한 변경 뒤에도 오래된 cache가 보였고 조직 구분이 빠진 key가 발견됐다.

확인 질문: 요청이 production인지 staging인지, `age_seconds`의 실제 값이 얼마인지 먼저 확인합니다.

진단: cache key의 tenant와 permission revision을 확인하고 영향을 받은 key만 무효화한다.

비교 기준: [현행 설정](../../config/production.json)의 `cache_seconds` = `300`.

설명할 이유: 동일 조건 리포트는 5분간 재사용한다. tenant와 권한 버전이 cache key에 포함된다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## aggregate group privacy

문의 유형: `aggregate group privacy`가 예상과 다르게 동작합니다.

증상: 한 명뿐인 팀의 통계가 개인 활동 내역처럼 표시됐다.

확인 질문: 요청이 production인지 staging인지, `members`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 그룹 크기를 확인하고 작은 그룹은 상위 집계로 합친다.

비교 기준: [현행 설정](../../config/production.json)의 `minimum_group` = `5`.

설명할 이유: 이 데모의 집계는 5명 미만 그룹을 숨긴다. 개인정보 보호의 일반적인 충분조건으로 해석하지 않는다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## report query timeout

문의 유형: `report query timeout`가 예상과 다르게 동작합니다.

증상: 30초까지 실행되는 조회가 운영 DB 커넥션을 오래 점유해 같은 시각의 다른 리포트 요청까지 함께 느려졌다.

확인 질문: 요청이 production인지 staging인지, `elapsed_seconds`의 실제 값이 얼마인지 먼저 확인합니다.

진단: query 상태와 오류 코드를 확인하고 마지막 정상 결과와 구분해서 표시한다.

비교 기준: [현행 설정](../../config/production.json)의 `query_seconds` = `8`.

설명할 이유: 느린 조회는 8초에 중단하고 비동기 분석을 안내한다. timeout을 빈 데이터로 표시하지 않는다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.
