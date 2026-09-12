---
tags: [slateharbor, files, current]
---
# Slateharbor 파일 관리: 지원 진단

> 합성 프로젝트 기록. 상태: current. 기준일: 2026-08-24. 실제 운영 지침이 아닙니다.

## file upload limit

문의 유형: `file upload limit`가 예상과 다르게 동작합니다.

증상: INC-FILES-12에서 동시 대형 업로드가 작은 첨부 요청까지 OOM으로 실패시켰다.

확인 질문: 요청이 production인지 staging인지, `size_mb`의 실제 값이 얼마인지 먼저 확인합니다.

진단: content length와 실제 수신량을 비교하고 multipart upload 상태를 조회한다.

비교 기준: [현행 설정](../../config/production.json)의 `upload_mb` = `25`.

설명할 이유: 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## signed URL lifetime

문의 유형: `signed URL lifetime`가 예상과 다르게 동작합니다.

증상: 외부로 전달된 링크가 공유 해제 뒤에도 오래 동작했다.

확인 질문: 요청이 production인지 staging인지, `age_minutes`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 링크 발급 시각과 공유 해제 시각을 비교하고 신규 URL 발급을 중단한다.

비교 기준: [현행 설정](../../config/production.json)의 `signed_url_minutes` = `10`.

설명할 이유: 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## trash retention

문의 유형: `trash retention`가 예상과 다르게 동작합니다.

증상: 실수 삭제를 일주일 뒤 발견해 복구 기간을 놓친 조직이 있었다.

확인 질문: 요청이 production인지 staging인지, `deleted_days`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 삭제 표시와 실제 blob 존재를 확인하고 hold 여부부터 조회한다.

비교 기준: [현행 설정](../../config/production.json)의 `retention_days` = `30`.

설명할 이유: 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## malware scan timeout

문의 유형: `malware scan timeout`가 예상과 다르게 동작합니다.

증상: timeout을 clean으로 취급한 경로 때문에 검사가 끝나지 않은 파일이 공개됐다.

확인 질문: 요청이 production인지 staging인지, `elapsed_seconds`의 실제 값이 얼마인지 먼저 확인합니다.

진단: scan 상태가 pending인지 확인하고 재검사 큐로 보낸다.

비교 기준: [현행 설정](../../config/production.json)의 `scan_seconds` = `120`.

설명할 이유: 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## document preview

문의 유형: `document preview`가 예상과 다르게 동작합니다.

증상: 긴 PDF 한 개가 preview worker를 점유해 이미지 썸네일까지 지연됐다.

확인 질문: 요청이 production인지 staging인지, `pages`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 쪽 수와 preview job 종류를 확인하고 대형 변환을 별도 큐로 보낸다.

비교 기준: [현행 설정](../../config/production.json)의 `preview_pages` = `20`.

설명할 이유: 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.

## multipart chunk

문의 유형: `multipart chunk`가 예상과 다르게 동작합니다.

증상: 큰 조각 재전송이 느린 네트워크에서 계속 timeout됐다.

확인 질문: 요청이 production인지 staging인지, `part_mb`의 실제 값이 얼마인지 먼저 확인합니다.

진단: 실패한 part 번호와 checksum을 확인하고 해당 조각만 재전송한다.

비교 기준: [현행 설정](../../config/production.json)의 `chunk_mb` = `8`.

설명할 이유: 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다. [구현](../../src/policy.py)과 다르면 설정 로딩부터 조사합니다.
