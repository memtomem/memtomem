> Synthetic content for search regression testing — verify before adopting as runbook.

## 2023-11-12 AWS 액세스 키 유출 — GitHub Actions 로그 노출

<!-- primary: security/secrets -->
<!-- secondary: ci_cd/pipeline, security/incident -->

2023년 11월 12일 14:30 KST, GitHub Actions 빌드 로그를 통해 하드코딩된 AWS 액세스 키 식별자(`<SYNTHETIC_AWS_KEY_ID>`)가 노출되는 가상 보안 사고가 발생했습니다. 조사 결과, 개발 환경 CI/CD 파이프라인에서 환경 변수를 마스킹하지 않아 발생한 유출로 확인되었습니다. 즉시 해당 키를 폐기(Revoke)하고 임시 자격 증명 기반의 OIDC를 도입하여 문제를 완화했습니다. 현재는 모든 워크플로우를 업데이트하여 `aws-actions/configure-aws-credentials@v4` 액션만 사용하도록 강제하고 있습니다.

## 2024-02-08 운영 네임스페이스 삭제 — cluster-admin 잔존 권한

<!-- primary: auth/rbac -->
<!-- secondary: security/access_control -->

2024년 2월 8일 09:15 UTC, 과도하게 부여된 권한으로 인해 운영 환경의 네임스페이스가 삭제되는 장애가 일어났습니다. 근본 원인은 이전 마이그레이션 작업 중 임시로 부여되었던 `cluster-admin` 권한이 회수되지 않은 상태에서, 작업자가 실수로 잘못된 타겟에 `kubectl delete -f drop-namespace.yaml` 명령을 실행한 것이었습니다. 피해 복구 후 즉시 해당 사용자의 `RoleBinding`을 최소 권한 원칙에 맞게 `view` 롤로 축소했습니다. 향후 모든 권한 부여는 24시간 TTL을 가진 임시 승인 시스템을 거치도록 정책을 변경했습니다.

## 2021-12-11 Log4j CVE-2021-44228 악성 페이로드 침해

<!-- primary: security/vulnerability -->
<!-- secondary: k8s/networking -->

2021년 12월 11일 22:00 KST, 사내 레거시 API 서버군에서 비정상적인 아웃바운드 네트워크 트래픽이 감지되었습니다. 분석 결과, `log4j-core:2.14.1` 라이브러리의 취약점(`CVE-2021-44228`)을 노리고 `X-Api-Version` 헤더에 삽입된 악성 페이로드가 WAF 필터링을 우회하여 실행된 것으로 밝혀졌습니다. 침해된 파드 12개를 즉각 격리 조치한 뒤, 모든 컨테이너 이미지를 Log4j 2.17.0 버전으로 긴급 패치하여 배포했습니다. 이후 쿠버네티스 `Egress` `NetworkPolicy`를 기본 차단(Default Deny)으로 변경하여 추가적인 데이터 유출 경로를 통제했습니다.

## 2024-05-19 PgBouncer 평문 자격 증명 — `sslmode=disable` 구성

<!-- primary: security/encryption -->
<!-- secondary: postgres/connection_pool, networking/tls -->

2024년 5월 19일 03:00 UTC 보안 감사 중, PgBouncer와 PostgreSQL 백엔드 간의 내부 통신 구간에서 평문 자격 증명이 노출되었음을 확인했습니다. 초기 설정 당시 `pgbouncer.ini` 파일에서 `server_tls_sslmode`가 `disable`로 구성되어 발생한 데이터 암호화 부재가 근본 원인이었습니다. 즉시 데이터베이스 노드를 재시작하여 `pg_hba.conf`의 모든 원격 접속 허용 항목을 `host`에서 `hostssl`로 강제 전환했습니다. 또한 `client_tls_sslmode=require` 설정을 모든 커넥션 풀러에 적용하여 향후 암호화되지 않은 세션은 원천 차단되도록 수정했습니다.
