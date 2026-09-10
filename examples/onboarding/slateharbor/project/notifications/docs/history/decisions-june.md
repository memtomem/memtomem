---
tags: [slateharbor, notifications, superseded]
---
# Slateharbor 알림: 대체된 6월 결정

> 합성 프로젝트 기록. 상태: superseded. 기준일: 2026-06-01. 실제 운영 지침이 아닙니다.

## notification retry policy

상태: superseded. 6월의 `notification retry policy` 값은 `12`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: INC-NOTIFY-31에서 공급자 429 이후 12회 즉시 재시도가 retry storm을 만들었다. 결제 webhook의 3회 제한과는 별도 정책이다.

8월 결정으로 대체: `5`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## notification backoff

상태: superseded. 6월의 `notification backoff` 값은 `50`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 같은 시각에 깨어난 worker들이 공급자 quota를 다시 소진했다.

8월 결정으로 대체: `250`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## digest batch

상태: superseded. 6월의 `digest batch` 값은 `100`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 주간 digest가 실시간 초대 메일보다 먼저 큐를 독점했다.

8월 결정으로 대체: `40`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## delivery deduplication

상태: superseded. 6월의 `delivery deduplication` 값은 `6`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 공급자 응답 유실 뒤 늦게 재시도한 알림이 두 번 배달됐다.

8월 결정으로 대체: `24`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## email payload

상태: superseded. 6월의 `email payload` 값은 `256`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 인라인 이미지가 메일 공급자 크기 제한을 넘어 전체 batch가 거절됐다.

8월 결정으로 대체: `128`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## notification queue age

상태: superseded. 6월의 `notification queue age` 값은 `60`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 낮은 처리량의 tenant 큐가 작아도 오래 대기하는데 경보가 없었다.

8월 결정으로 대체: `15`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.
