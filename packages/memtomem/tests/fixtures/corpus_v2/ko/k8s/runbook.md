> Synthetic content for search regression testing — verify before adopting as runbook.

## PVC 용량 확장 절차 (kubectl edit pvc)

<!-- primary: k8s/storage -->
<!-- secondary: -->

kubernetes PVC 용량을 확장하려면 먼저 `StorageClass`에서 `allowVolumeExpansion: true`를 확인하십시오. `kubectl edit pvc mysql-data -n prod` 명령을 실행하여 `spec.resources.requests.storage` 값을 수정합니다. 이후 `kubectl get pvc -w`를 통해 `FileSystemResizePending` 상태가 해제되고 용량 변경이 반영되었는지 검증하십시오.

## 신규 Ingress v1 매니페스트 적용 및 검증

<!-- primary: k8s/networking -->
<!-- secondary: -->

새로운 kubernetes Ingress 설정을 적용하려면 `apiVersion: networking.k8s.io/v1` 형식을 준수하여 매니페스트를 작성하십시오. `kubectl apply -f ingress-api.yaml`을 실행한 뒤, `kubectl logs -l app.kubernetes.io/name=ingress-nginx -n ingress-nginx`를 통해 설정 오류 여부를 체크합니다. TLS 설정이 포함된 경우 `Secret` 객체가 올바른 네임스페이스에 존재하는지 반드시 확인하십시오.

## HPA CPU 기반 자동 스케일링 설정

<!-- primary: k8s/scaling -->
<!-- secondary: observability/metrics -->

kubernetes HPA를 설정하여 CPU 부하에 따라 복제본 수를 자동 조절하십시오. `kubectl autoscale deployment api --cpu-percent=70 --min=3 --max=10` 명령을 실행합니다. 설정 후 `kubectl get hpa api-hpa -o yaml`을 통해 `currentMetrics`가 `metrics-server`로부터 정상적으로 수집되고 있는지 확인하십시오.

## GPU 워크로드 nodeSelector 배치 절차

<!-- primary: k8s/scheduling -->
<!-- secondary: -->

kubernetes GPU 워크로드를 특정 노드에 배치하기 위해 `nodeSelector`를 설정하십시오. 먼저 `kubectl label nodes gpu-node-01 hardware=nvidia-t4` 명령으로 노드에 라벨을 부여합니다. Pod 스펙의 `nodeSelector` 필드에 `hardware: nvidia-t4`를 추가하고 배포하여 스케줄링 결과를 `kubectl get pod -o wide`로 검증하십시오.
