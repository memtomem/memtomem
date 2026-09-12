---
tags: [slateharbor, files, superseded]
---
# Slateharbor 파일 관리: 대체된 6월 결정

> 합성 프로젝트 기록. 상태: superseded. 기준일: 2026-06-01. 실제 운영 지침이 아닙니다.

## file upload limit

상태: superseded. 6월의 `file upload limit` 값은 `100`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: INC-FILES-12에서 동시 대형 업로드가 작은 첨부 요청까지 OOM으로 실패시켰다.

8월 결정으로 대체: `25`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## signed URL lifetime

상태: superseded. 6월의 `signed URL lifetime` 값은 `60`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 외부로 전달된 링크가 공유 해제 뒤에도 오래 동작했다.

8월 결정으로 대체: `10`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## trash retention

상태: superseded. 6월의 `trash retention` 값은 `7`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 실수 삭제를 일주일 뒤 발견해 복구 기간을 놓친 조직이 있었다.

8월 결정으로 대체: `30`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## malware scan timeout

상태: superseded. 6월의 `malware scan timeout` 값은 `30`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: timeout을 clean으로 취급한 경로 때문에 검사가 끝나지 않은 파일이 공개됐다.

8월 결정으로 대체: `120`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## document preview

상태: superseded. 6월의 `document preview` 값은 `100`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 긴 PDF 한 개가 preview worker를 점유해 이미지 썸네일까지 지연됐다.

8월 결정으로 대체: `20`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.

## multipart chunk

상태: superseded. 6월의 `multipart chunk` 값은 `32`였습니다. 이 문서는 당시의 판단을 보존합니다.

당시 가정: 초기 pilot에서는 요청 규모와 client 종류가 제한돼 있었고 예외 처리는 지원팀이 수동으로 담당했습니다.

가정이 깨진 관찰: 큰 조각 재전송이 느린 네트워크에서 계속 timeout됐다.

8월 결정으로 대체: `8`. [현행 결정](../decisions/decision-current.md)을 확인하고 이 값을 운영에 복사하지 마세요.
