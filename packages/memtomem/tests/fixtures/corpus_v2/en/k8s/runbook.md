> Synthetic content for search regression testing — verify before adopting as runbook.

## Deployment image update with rollout status monitoring

<!-- primary: k8s/rollout -->
<!-- secondary: -->

Update the deployment image using `kubectl set image deployment/frontend-v1 container=web:2.1.0`. Monitor the progression by running `kubectl rollout status deployment/frontend-v1`. If the rollout stalls, verify the `progressDeadlineSeconds` field in your manifest and use `kubectl rollout undo deployment/frontend-v1` to revert to the previous stable revision immediately.

## Apply restricted NetworkPolicy for database namespace

<!-- primary: k8s/networking -->
<!-- secondary: security/access_control -->

Isolate the database namespace by applying a restricted `NetworkPolicy`. Run `kubectl apply -f db-policy.yaml` with a spec that only allows ingress from the `app-srv` label on port 5432. Validate the connectivity from an unauthorized pod using `kubectl exec` to run `nc -zv db-service 5432`, ensuring the connection is timed out as expected.

## Taint spot nodes to isolate critical workloads

<!-- primary: k8s/scheduling -->
<!-- secondary: cost_optimization/compute -->

To prevent workloads from running on spot nodes during critical windows, apply a taint using `kubectl taint nodes node-spot-01 workload=unstable:NoSchedule`. In your deployment YAML, ensure no `tolerations` match this key unless specifically required. Verify that pods are scheduled only on on-demand nodes by checking the output of `kubectl get nodes -l capacity-type=on-demand`.

## VerticalPodAutoscaler for memory right-sizing

<!-- primary: k8s/scaling -->
<!-- secondary: observability/metrics -->

Configure a VerticalPodAutoscaler to right-size your memory limits. Deploy the VPA object with `updateMode: Auto` and a target reference to your Deployment. After 24 hours of traffic, run `kubectl get vpa my-app-vpa -o jsonpath='{.status.recommendation}'` to view the suggested resource requests and ensure they align with the `container_memory_working_set_bytes` metric.
