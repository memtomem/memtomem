> Synthetic content for search regression testing — verify before adopting as runbook.

## CrashLoopBackOff Pod 로그 + LivenessProbe 진단

<!-- primary: k8s/rollout -->
<!-- secondary: -->

kubernetes Pod가 `CrashLoopBackOff` 상태에 빠진 경우, 먼저 `kubectl logs <pod-name> --previous` 명령으로 이전 컨테이너의 종료 로그를 확인하십시오. 원인이 환경 변수 설정 오류라면 `configMap` 정의를 검증해야 합니다. 만약 `LivenessProbe` 실패가 원인이라면 `kubectl describe pod`에서 `Events` 섹션의 'Unhealthy' 메시지를 확인하여 타임아웃 설정을 튜닝하십시오.

## 서비스 DNS 해결 실패 진단 (CoreDNS / kube-proxy)

<!-- primary: k8s/networking -->
<!-- secondary: networking/dns -->

kubernetes 서비스 간 DNS 해결이 실패한다면 `kubectl exec -it <pod-name> -- nslookup <service-name>`을 실행하여 응답을 확인하십시오. `CoreDNS` 설정 오류가 의심될 경우 `kubectl logs -n kube-system -l k8s-app=kube-dns` 명령으로 로그를 분석합니다. `kube-proxy`가 최신 iptables 규칙을 반영하지 못해 발생하는 연결 지연인지 확인하려면 `ipvsadm -Ln` 또는 관련 메트릭을 점검하십시오.

## PVC Pending 상태 FailedBinding 진단

<!-- primary: k8s/storage -->
<!-- secondary: k8s/scheduling -->

kubernetes PVC가 `Pending` 상태로 머물러 있다면 `kubectl describe pvc <pvc-name>`을 실행하여 `Events`를 확인하십시오. 'FailedBinding' 메시지와 함께 노드 어피니티 충돌이 보인다면, `StorageClass`의 `volumeBindingMode` 설정을 점검해야 합니다. 특히 클라우드 환경에서는 PVC가 요구하는 Zone에 가용한 PV가 있는지 또는 CSI 드라이버가 정상 작동 중인지 `kubectl get csidriver`로 확인하십시오.

## Insufficient cpu 스케줄링 실패 대응

<!-- primary: k8s/scheduling -->
<!-- secondary: cost_optimization/compute -->

kubernetes Pod가 스케줄링되지 않고 `Insufficient cpu` 메시지가 발생하면 `kubectl describe node`로 노드별 리소스 할당 현황을 확인하십시오. 원인은 실제 물리 자원 부족이거나 `ResourceQuota` 제한 때문일 수 있습니다. 불필요하게 높은 `requests` 설정을 가진 Pod를 찾아 조정하거나, `cluster-autoscaler`가 새 노드를 프로비저닝하지 못하는 이유를 로그에서 찾아 해결하십시오.
