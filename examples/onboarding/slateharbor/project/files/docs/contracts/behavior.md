---
tags: [slateharbor, files, current]
---
# Slateharbor 파일 관리: 동작 계약

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## file upload limit

입력: `size_mb`. `file upload limit`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `upload_mb`입니다.

승인 예: `10`. 거절 예: `50`. 기준은 [운영 설정](../../config/production.json)의 `25`입니다.

업무 의미: 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다.

잘못된 적용 사례: INC-FILES-12에서 동시 대형 업로드가 작은 첨부 요청까지 OOM으로 실패시켰다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## signed URL lifetime

입력: `age_minutes`. `signed URL lifetime`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `signed_url_minutes`입니다.

승인 예: `5`. 거절 예: `20`. 기준은 [운영 설정](../../config/production.json)의 `10`입니다.

업무 의미: 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다.

잘못된 적용 사례: 외부로 전달된 링크가 공유 해제 뒤에도 오래 동작했다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## trash retention

입력: `deleted_days`. `trash retention`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `retention_days`입니다.

승인 예: `14`. 거절 예: `40`. 기준은 [운영 설정](../../config/production.json)의 `30`입니다.

업무 의미: 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다.

잘못된 적용 사례: 실수 삭제를 일주일 뒤 발견해 복구 기간을 놓친 조직이 있었다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## malware scan timeout

입력: `elapsed_seconds`. `malware scan timeout`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `scan_seconds`입니다.

승인 예: `60`. 거절 예: `180`. 기준은 [운영 설정](../../config/production.json)의 `120`입니다.

업무 의미: 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다.

잘못된 적용 사례: timeout을 clean으로 취급한 경로 때문에 검사가 끝나지 않은 파일이 공개됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## document preview

입력: `pages`. `document preview`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `preview_pages`입니다.

승인 예: `10`. 거절 예: `30`. 기준은 [운영 설정](../../config/production.json)의 `20`입니다.

업무 의미: 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다.

잘못된 적용 사례: 긴 PDF 한 개가 preview worker를 점유해 이미지 썸네일까지 지연됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.

## multipart chunk

입력: `part_mb`. `multipart chunk`를 평가하는 함수는 [policy.py](../../src/policy.py)의 `chunk_mb`입니다.

승인 예: `4`. 거절 예: `16`. 기준은 [운영 설정](../../config/production.json)의 `8`입니다.

업무 의미: 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다.

잘못된 적용 사례: 큰 조각 재전송이 느린 네트워크에서 계속 timeout됐다.

이 코드는 정책 판단만 구현합니다. 실제 HTTP 인증이나 외부 서비스 호출을 제공하지 않습니다.
