> Synthetic content for search regression testing — verify before adopting as runbook.

## 2026-04-10 Calico MTU 설정 오류로 패킷 유실

<!-- primary: k8s/networking -->
<!-- secondary: -->

2026-04-10 14:00 UTC경, kubernetes CNI 설정 오류로 인해 전체 서비스의 30%가 패킷 유실을 겪었습니다. 조사 결과, Calico의 MTU 값이 물리 인터페이스보다 높게 설정되어 거대 패킷이 드롭된 것이 근본 원인이었습니다. 우리는 `kubectl patch configmap/calico-config`를 통해 MTU를 1450으로 수정하여 해결했으며, 향후 재발 방지를 위해 네트워크 가용성 프로브를 추가했습니다.

## 2026-03-25 프로모션 트래픽 HPA minReplicas 부족

<!-- primary: k8s/scaling -->
<!-- secondary: k8s/scheduling -->

2026-03-25일, kubernetes 프로모션 트래픽 급증 시 HPA의 `minReplicas` 설정이 너무 낮아 연쇄적인 노드 과부하가 발생했습니다. `cluster-autoscaler`가 새 노드를 준비하는 동안 기존 Pod들이 OOMKilled 처리되며 스케줄링 루프에 빠졌습니다. 대응책으로 핵심 API의 최소 복제본 수를 10으로 상향 조정하고, `kube_pod_container_status_waiting_reason` 지표에 대한 긴급 알림을 설정했습니다.

## 2026-04-05 reclaimPolicy Delete PVC 실수 삭제

<!-- primary: k8s/storage -->
<!-- secondary: postgres/replication -->

2026-04-05일 야간 작업 중, kubernetes `reclaimPolicy`가 `Delete`로 설정된 `StorageClass`를 사용하던 중 실수로 DB PVC가 삭제되어 데이터 유실 위기가 있었습니다. 다행히 Postgres WAL 아카이브를 통해 복구했으나, 영구적인 위험을 제거하기 위해 모든 배포 환경의 정책을 `Retain`으로 변경했습니다. 또한 `kubectl delete` 명령에 대한 RBAC 권한을 재검토하여 데이터베이스 관련 객체의 삭제 권한을 제한했습니다.

## 2026-04-12 maxUnavailable 50% 배포 장애

<!-- primary: k8s/rollout -->
<!-- secondary: ci_cd/deployment -->

지난 2026-04-12일, kubernetes `maxUnavailable` 설정을 50%로 설정한 상태에서 배포를 진행하다가 새 버전의 런타임 오류로 인해 가용성이 절반으로 급감했습니다. `progressDeadlineSeconds` 이전에 수동 롤백을 수행했으나 약 15분간의 장애가 지속되었습니다. 이를 교훈 삼아 배포 전략을 `maxUnavailable: 1`로 강화하고, ArgoCD의 `AnalysisRun`을 도입하여 카나리 배포 시 자동 롤백이 가능하도록 개선했습니다.
