---
tags: [slateharbor, files, current]
---
# Slateharbor 파일 관리: 채택 결정

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## file upload limit

결정 FILES-01: `file upload limit`의 현재 값은 `25`입니다.

선택 이유: 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다.

대안 검토: 이전 값 `100`는 아래 장애 조건을 만족하지 못했습니다. INC-FILES-12에서 동시 대형 업로드가 작은 첨부 요청까지 OOM으로 실패시켰다.

적용: [정책 구현](../../src/policy.py)의 `upload_mb`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: content length와 실제 수신량을 비교하고 multipart upload 상태를 조회한다.

## signed URL lifetime

결정 FILES-02: `signed URL lifetime`의 현재 값은 `10`입니다.

선택 이유: 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다.

대안 검토: 이전 값 `60`는 아래 장애 조건을 만족하지 못했습니다. 외부로 전달된 링크가 공유 해제 뒤에도 오래 동작했다.

적용: [정책 구현](../../src/policy.py)의 `signed_url_minutes`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 링크 발급 시각과 공유 해제 시각을 비교하고 신규 URL 발급을 중단한다.

## trash retention

결정 FILES-03: `trash retention`의 현재 값은 `30`입니다.

선택 이유: 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다.

대안 검토: 이전 값 `7`는 아래 장애 조건을 만족하지 못했습니다. 실수 삭제를 일주일 뒤 발견해 복구 기간을 놓친 조직이 있었다.

적용: [정책 구현](../../src/policy.py)의 `retention_days`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 삭제 표시와 실제 blob 존재를 확인하고 hold 여부부터 조회한다.

## malware scan timeout

결정 FILES-04: `malware scan timeout`의 현재 값은 `120`입니다.

선택 이유: 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다.

대안 검토: 이전 값 `30`는 아래 장애 조건을 만족하지 못했습니다. timeout을 clean으로 취급한 경로 때문에 검사가 끝나지 않은 파일이 공개됐다.

적용: [정책 구현](../../src/policy.py)의 `scan_seconds`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: scan 상태가 pending인지 확인하고 재검사 큐로 보낸다.

## document preview

결정 FILES-05: `document preview`의 현재 값은 `20`입니다.

선택 이유: 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다.

대안 검토: 이전 값 `100`는 아래 장애 조건을 만족하지 못했습니다. 긴 PDF 한 개가 preview worker를 점유해 이미지 썸네일까지 지연됐다.

적용: [정책 구현](../../src/policy.py)의 `preview_pages`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 쪽 수와 preview job 종류를 확인하고 대형 변환을 별도 큐로 보낸다.

## multipart chunk

결정 FILES-06: `multipart chunk`의 현재 값은 `8`입니다.

선택 이유: 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다.

대안 검토: 이전 값 `32`는 아래 장애 조건을 만족하지 못했습니다. 큰 조각 재전송이 느린 네트워크에서 계속 timeout됐다.

적용: [정책 구현](../../src/policy.py)의 `chunk_mb`와 [운영 설정](../../config/production.json)의 같은 키를 함께 확인합니다.

검증: 실패한 part 번호와 checksum을 확인하고 해당 조각만 재전송한다.
