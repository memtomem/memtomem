---
tags: [slateharbor, jobs, superseded]
---
# Slateharbor 백그라운드 작업: 대체된 6월 결정

> 합성 프로젝트 기록. 상태: superseded. 기준일: 2026-06-01. 실제 운영 지침이 아닙니다.

## worker lease recovery

상태: superseded. 6월의 `worker lease recovery` 값은 `30`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: INC-JOBS-22에서 GC pause로 heartbeat가 늦자 살아 있는 worker의 작업을 다른 worker가 다시 수행했다.

8월 결정으로 대체: `90`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## worker concurrency

상태: superseded. 6월의 `worker concurrency` 값은 `16`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 과도한 병렬 압축이 이벤트 루프를 막아 lease가 연달아 만료됐다.

8월 결정으로 대체: `4`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## checkpoint interval

상태: superseded. 6월의 `checkpoint interval` 값은 `60`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 메모리상의 진행률만 저장해 재시작 후 누락된 출력 구간이 생겼다.

8월 결정으로 대체: `20`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## job retry budget

상태: superseded. 6월의 `job retry budget` 값은 `6`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 손상된 파일 하나가 worker 시간을 반복 소비하며 정상 작업을 밀어냈다.

8월 결정으로 대체: `2`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## worker shutdown grace

상태: superseded. 6월의 `worker shutdown grace` 값은 `10`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 즉시 종료 때문에 결과는 썼지만 완료 표시가 없어 작업이 다시 실행됐다.

8월 결정으로 대체: `45`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## job admission

상태: superseded. 6월의 `job admission` 값은 `1000`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 대량 업로드가 오래된 작업의 처리 약속을 깨뜨렸다.

8월 결정으로 대체: `200`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.
