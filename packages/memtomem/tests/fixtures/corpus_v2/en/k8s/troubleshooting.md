> Synthetic content for search regression testing — verify before adopting as runbook.

## HPA FailedGetResourceMetric diagnosis (metrics-server)

<!-- primary: k8s/scaling -->
<!-- secondary: observability/metrics -->

If the HorizontalPodAutoscaler is not triggering replicas, run `kubectl describe hpa <name>` to check for the 'FailedGetResourceMetric' event. This typically indicates that `metrics-server` is missing or the pod labels don't match the deployment selector. Verify the current usage via `kubectl top pods` and ensure the container spec includes the necessary resource `requests` for the HPA to calculate target percentages.

## 504 Gateway Timeout at ingress-nginx upstream

<!-- primary: k8s/networking -->
<!-- secondary: -->

When seeing 504 Gateway Timeouts at the Ingress level, run `kubectl logs -n ingress-nginx -l app.kubernetes.io/instance=ingress-nginx` to find upstream timeout errors. Check if the backend Service is using a headless configuration or if the `proxy-read-timeout` annotation needs adjustment. Confirm the pod readiness using `kubectl get endpoints <service-name>` to ensure traffic is being routed to active containers.

## Multi-Attach error blocking rolling update

<!-- primary: k8s/rollout -->
<!-- secondary: k8s/storage -->

A deployment stuck in `WaitingForPod` during a rolling update often points to a `Multi-Attach` error for PersistentVolumes. Run `kubectl describe pod` to see if the volume is still attached to the old terminating pod on a different node. Check the `pv.kubernetes.io/bound-by-controller` annotation and, if necessary, manually delete the orphan `VolumeAttachment` object to free the lock.

## Pods Pending with NotReady nodes (CNI / kubelet)

<!-- primary: k8s/scheduling -->
<!-- secondary: k8s/networking -->

If pods are stuck in `Pending` with a 'nodes are unreachable' message, check the node status via `kubectl get nodes`. A `NotReady` status might be caused by `kubelet` losing connection to the API server or a CNI plugin failure. Run `journalctl -u kubelet` on the affected node to diagnose underlying system errors or CIDR exhaustion that prevents the node from becoming `Ready`.
