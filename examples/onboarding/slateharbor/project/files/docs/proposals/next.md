---
tags: [slateharbor, files, proposed]
---
# Slateharbor 파일 관리: 검토 중 제안

> 합성 프로젝트 기록. 상태: proposed. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## file upload limit

상태: proposed. `file upload limit` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: INC-FILES-12에서 동시 대형 업로드가 작은 첨부 요청까지 OOM으로 실패시켰다.

제안: TASK-FILES-33: 중단된 multipart session의 정리 작업을 추가한다.

현재 계약: `upload_mb` = `25`. 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## signed URL lifetime

상태: proposed. `signed URL lifetime` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 외부로 전달된 링크가 공유 해제 뒤에도 오래 동작했다.

제안: TASK-FILES-34: 만료 응답에서 로그인 이동 경로를 확인한다.

현재 계약: `signed_url_minutes` = `10`. 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## trash retention

상태: proposed. `trash retention` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 실수 삭제를 일주일 뒤 발견해 복구 기간을 놓친 조직이 있었다.

제안: TASK-FILES-35: 복원 시 원래 폴더가 없을 때의 동작을 정한다.

현재 계약: `retention_days` = `30`. 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## malware scan timeout

상태: proposed. `malware scan timeout` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: timeout을 clean으로 취급한 경로 때문에 검사가 끝나지 않은 파일이 공개됐다.

제안: TASK-FILES-36: unknown 상태를 UI에서 별도로 표시한다.

현재 계약: `scan_seconds` = `120`. 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## document preview

상태: proposed. `document preview` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 긴 PDF 한 개가 preview worker를 점유해 이미지 썸네일까지 지연됐다.

제안: TASK-FILES-37: preview 제한 안내에 원본 다운로드를 연결한다.

현재 계약: `preview_pages` = `20`. 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.

## multipart chunk

상태: proposed. `multipart chunk` 관찰 지표를 tenant별로 나누는 개선안을 검토 중입니다. 운영 값은 바꾸지 않았습니다.

문제: 큰 조각 재전송이 느린 네트워크에서 계속 timeout됐다.

제안: TASK-FILES-38: 중복 part 완료 이벤트를 idempotent하게 처리한다.

현재 계약: `chunk_mb` = `8`. 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다.

채택 조건: [현행 결정](../decisions/decision-current.md)의 조건을 지키고 별도 cohort에서 영향이 없는지 검증한 뒤 승인 기록을 남깁니다.
