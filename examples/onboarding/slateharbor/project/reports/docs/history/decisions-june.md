---
tags: [slateharbor, reports, superseded]
---
# Slateharbor 리포트: 대체된 6월 결정

> 합성 프로젝트 기록. 상태: superseded. 기준일: 2026-06-01. 실제 운영 지침이 아닙니다.

## report freshness

상태: superseded. 6월의 `report freshness` 값은 `60`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: INC-REPORT-08에서 어제 생성된 캐시가 최신 매출처럼 표시돼 운영자가 정산을 다시 요청했다.

8월 결정으로 대체: `15`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## CSV export rows

상태: superseded. 6월의 `CSV export rows` 값은 `50000`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 동기 요청 timeout 뒤 사용자가 다시 눌러 같은 export가 여러 번 만들어졌다.

8월 결정으로 대체: `10000`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## report window

상태: superseded. 6월의 `report window` 값은 `365`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 1년 범위의 조인 쿼리가 운영 DB의 온라인 트래픽을 밀어냈다.

8월 결정으로 대체: `90`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## report cache ttl

상태: superseded. 6월의 `report cache ttl` 값은 `1800`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 권한 변경 뒤에도 오래된 cache가 보였고 조직 구분이 빠진 key가 발견됐다.

8월 결정으로 대체: `300`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## aggregate group privacy

상태: superseded. 6월의 `aggregate group privacy` 값은 `1`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 한 명뿐인 팀의 통계가 개인 활동 내역처럼 표시됐다.

8월 결정으로 대체: `5`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## report query timeout

상태: superseded. 6월의 `report query timeout` 값은 `30`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 30초까지 실행되는 조회가 운영 DB 커넥션을 오래 점유해 같은 시각의 다른 리포트 요청까지 함께 느려졌다.

8월 결정으로 대체: `8`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.
