---
tags: [slateharbor, files, current]
---
# Slateharbor 파일 관리: 8월 변경 기록

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## file upload limit

8월 변경: `file upload limit` 값이 `100`에서 `25`로 바뀌었습니다.

관련 관찰: INC-FILES-12에서 동시 대형 업로드가 작은 첨부 요청까지 OOM으로 실패시켰다.

사용자에게 달라지는 점: 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다.

확인 절차: content length와 실제 수신량을 비교하고 multipart upload 상태를 조회한다.

추적 작업: TASK-FILES-33: 중단된 multipart session의 정리 작업을 추가한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## signed URL lifetime

8월 변경: `signed URL lifetime` 값이 `60`에서 `10`로 바뀌었습니다.

관련 관찰: 외부로 전달된 링크가 공유 해제 뒤에도 오래 동작했다.

사용자에게 달라지는 점: 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다.

확인 절차: 링크 발급 시각과 공유 해제 시각을 비교하고 신규 URL 발급을 중단한다.

추적 작업: TASK-FILES-34: 만료 응답에서 로그인 이동 경로를 확인한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## trash retention

8월 변경: `trash retention` 값이 `7`에서 `30`로 바뀌었습니다.

관련 관찰: 실수 삭제를 일주일 뒤 발견해 복구 기간을 놓친 조직이 있었다.

사용자에게 달라지는 점: 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다.

확인 절차: 삭제 표시와 실제 blob 존재를 확인하고 hold 여부부터 조회한다.

추적 작업: TASK-FILES-35: 복원 시 원래 폴더가 없을 때의 동작을 정한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## malware scan timeout

8월 변경: `malware scan timeout` 값이 `30`에서 `120`로 바뀌었습니다.

관련 관찰: timeout을 clean으로 취급한 경로 때문에 검사가 끝나지 않은 파일이 공개됐다.

사용자에게 달라지는 점: 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다.

확인 절차: scan 상태가 pending인지 확인하고 재검사 큐로 보낸다.

추적 작업: TASK-FILES-36: unknown 상태를 UI에서 별도로 표시한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## document preview

8월 변경: `document preview` 값이 `100`에서 `20`로 바뀌었습니다.

관련 관찰: 긴 PDF 한 개가 preview worker를 점유해 이미지 썸네일까지 지연됐다.

사용자에게 달라지는 점: 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다.

확인 절차: 쪽 수와 preview job 종류를 확인하고 대형 변환을 별도 큐로 보낸다.

추적 작업: TASK-FILES-37: preview 제한 안내에 원본 다운로드를 연결한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).

## multipart chunk

8월 변경: `multipart chunk` 값이 `32`에서 `8`로 바뀌었습니다.

관련 관찰: 큰 조각 재전송이 느린 네트워크에서 계속 timeout됐다.

사용자에게 달라지는 점: 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다.

확인 절차: 실패한 part 번호와 checksum을 확인하고 해당 조각만 재전송한다.

추적 작업: TASK-FILES-38: 중복 part 완료 이벤트를 idempotent하게 처리한다. [결정](../decisions/decision-current.md), [구현](../../src/policy.py).
