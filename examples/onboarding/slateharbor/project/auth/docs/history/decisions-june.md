---
tags: [slateharbor, auth, superseded]
---
# Slateharbor 인증: 대체된 6월 결정

> 합성 프로젝트 기록. 상태: superseded. 기준일: 2026-06-01. 실제 운영 지침이 아닙니다.

## legacy callback

상태: superseded. 6월의 `legacy callback` 값은 `False`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: INC-AUTH-17에서 신규 웹 로그인은 정상인데 구형 데스크톱만 callback 404가 발생했다. 웹 성공률 대시보드에는 장애가 드러나지 않았다.

8월 결정으로 대체: `True`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## session expiry

상태: superseded. 6월의 `session expiry` 값은 `120`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 교대 종료 후 같은 브라우저를 쓰던 다른 직원에게 이전 tenant 화면이 노출됐다.

8월 결정으로 대체: `60`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## login throttle

상태: superseded. 6월의 `login throttle` 값은 `10`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 계정당 10회를 허용하는 동안 자동화 도구가 유효 계정 목록을 추려냈고, IP 단위로 막자 같은 사무실의 정상 로그인까지 함께 차단됐다.

8월 결정으로 대체: `5`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## invite validity

상태: superseded. 6월의 `invite validity` 값은 `168`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 회수된 초대 메일의 링크가 일주일 뒤에도 사용됐다.

8월 결정으로 대체: `48`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## clock skew

상태: superseded. 6월의 `clock skew` 값은 `120`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 시간 동기화가 끊긴 worker가 만료된 세션을 허용했다.

8월 결정으로 대체: `30`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## recovery inventory

상태: superseded. 6월의 `recovery inventory` 값은 `4`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 복구 코드 4개를 모두 소진한 사용자가 재발급 전까지 로그인하지 못해 지원팀 수동 처리로 몰렸다.

8월 결정으로 대체: `8`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.
