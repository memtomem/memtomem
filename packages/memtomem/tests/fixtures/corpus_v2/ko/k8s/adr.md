> Synthetic content for search regression testing — verify before adopting as runbook.

## Calico 대신 Cilium CNI 도입 (eBPF 기반)

<!-- primary: k8s/networking -->
<!-- secondary: observability/metrics -->

우리는 kubernetes 네트워크 성능 최적화를 위해 Calico 대신 Cilium을 CNI로 선택했습니다. eBPF 기반의 데이터 평면을 사용함으로써 iptables의 오버헤드를 줄이고, `hubble_request_duration_seconds`와 같은 지표를 통해 서비스 간 통신 가시성을 확보하기 위함입니다. 다만, 기존 노드의 커널 버전을 5.10 이상으로 업그레이드해야 하는 운영 비용은 감수하기로 결정했습니다.

## 레거시 세션을 위한 Recreate 전략 채택

<!-- primary: k8s/rollout -->
<!-- secondary: k8s/networking -->

데이터 일관성이 중요한 레거시 kubernetes 세션 관리를 위해 `RollingUpdate` 대신 `Recreate` 전략을 채택했습니다. `maxSurge`를 0으로 설정하여 동시에 두 버전이 실행되는 것을 방지함으로써 DB 락 경합을 회피합니다. 배포 중 발생하는 일시적인 가동 중단(Downtime)은 Ingress의 커스텀 503 오류 페이지로 보완하는 트레이드오프를 수용했습니다.

## reclaimPolicy: Retain 데이터 보존 우선 결정

<!-- primary: k8s/storage -->
<!-- secondary: cost_optimization/storage -->

kubernetes 워크로드의 I/O 요구사항에 따라 `StorageClass`의 `reclaimPolicy`를 `Retain`으로 설정하기로 결정했습니다. 실수로 PVC가 삭제되더라도 실제 EBS 볼륨 데이터가 보존되도록 하여 데이터 복구 안전성을 높였습니다. 대신 사용되지 않는 볼륨에 대한 비용 최적화를 위해 `aws_ebs_volume_unused_count` 지표를 모니터링하여 수동으로 정리하는 프로세스를 도입합니다.

## podAntiAffinity 대신 topologySpreadConstraints 우선 사용

<!-- primary: k8s/scheduling -->
<!-- secondary: k8s/scaling -->

kubernetes 배치 작업의 효율적인 노드 점유를 위해 `podAntiAffinity`보다 `topologySpreadConstraints`를 우선적으로 사용합니다. `whenUnsatisfied: ScheduleAnyway` 설정을 통해 엄격한 제약으로 인한 스케줄링 실패를 방지하면서도 노드 간 균형 있는 배치를 유도했습니다. 이는 특정 존(Zone)의 가용량 부족 시에도 `k8s/scaling`이 원활하게 작동하도록 돕는 결정입니다.
