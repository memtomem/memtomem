---
tags: [slateharbor, files, current]
---
# Slateharbor 파일 관리: 작업 인계

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## file upload limit

작업: TASK-FILES-33: 중단된 multipart session의 정리 작업을 추가한다.

이미 확정: `file upload limit` = `25`. 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: content length와 실제 수신량을 비교하고 multipart upload 상태를 조회한다.

보류 사항: 원인 확인 없이 이전 `100`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## signed URL lifetime

작업: TASK-FILES-34: 만료 응답에서 로그인 이동 경로를 확인한다.

이미 확정: `signed URL lifetime` = `10`. 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 링크 발급 시각과 공유 해제 시각을 비교하고 신규 URL 발급을 중단한다.

보류 사항: 원인 확인 없이 이전 `60`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## trash retention

작업: TASK-FILES-35: 복원 시 원래 폴더가 없을 때의 동작을 정한다.

이미 확정: `trash retention` = `30`. 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 삭제 표시와 실제 blob 존재를 확인하고 hold 여부부터 조회한다.

보류 사항: 원인 확인 없이 이전 `7`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## malware scan timeout

작업: TASK-FILES-36: unknown 상태를 UI에서 별도로 표시한다.

이미 확정: `malware scan timeout` = `120`. 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: scan 상태가 pending인지 확인하고 재검사 큐로 보낸다.

보류 사항: 원인 확인 없이 이전 `30`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## document preview

작업: TASK-FILES-37: preview 제한 안내에 원본 다운로드를 연결한다.

이미 확정: `document preview` = `20`. 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 쪽 수와 preview job 종류를 확인하고 대형 변환을 별도 큐로 보낸다.

보류 사항: 원인 확인 없이 이전 `100`로 되돌리는 작업은 제안 단계로 남겨 둡니다.

## multipart chunk

작업: TASK-FILES-38: 중복 part 완료 이벤트를 idempotent하게 처리한다.

이미 확정: `multipart chunk` = `8`. 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다.

다음 사람이 볼 곳: [채택 결정](../decisions/decision-current.md), [구현](../../src/policy.py), [운영 설정](../../config/production.json).

재개 순서: 실패한 part 번호와 checksum을 확인하고 해당 조각만 재전송한다.

보류 사항: 원인 확인 없이 이전 `32`로 되돌리는 작업은 제안 단계로 남겨 둡니다.
