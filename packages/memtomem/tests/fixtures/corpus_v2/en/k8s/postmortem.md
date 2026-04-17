> Synthetic content for search regression testing — verify before adopting as runbook.

## 2026-04-15 Production rollout failure missing ImagePullSecret

<!-- primary: k8s/rollout -->
<!-- secondary: security/secrets -->

On 2026-04-15 at 09:20 UTC, the production API rollout failed due to a missing `ImagePullSecret`. The investigation showed that the CI pipeline did not propagate the updated credentials to the `prod` namespace. We mitigated the issue by manually creating the secret and running `kubectl rollout retry deployment/api`. Post-incident, we added a validation step in our Helm charts to check for secret existence before deployment.

## 2026-04-02 CoreDNS ConfigMap syntax broke DNS for 45 minutes

<!-- primary: k8s/networking -->
<!-- secondary: networking/dns -->

A major DNS resolution failure occurred on 2026-04-02 when a `ConfigMap` update for CoreDNS contained a syntax error. This led to a `CrashLoopBackOff` of all `kube-dns` pods, breaking internal service discovery for 45 minutes. We restored service by reverting to the previous `ConfigMap` version and have since implemented a pre-commit hook that runs `coredns -conf-check` to validate configuration changes.

## 2026-03-30 SSD volume quota exhaustion stuck Pending pods

<!-- primary: k8s/storage -->
<!-- secondary: k8s/scheduling -->

On 2026-03-30, several pods remained in `Pending` state for over 2 hours due to a quota exhaustion on the cloud provider's side for SSD volumes. The `PersistentVolumeClaim` events showed 'DiskLimitExceeded', but alerting was delayed. We resolved the capacity issue by requesting a limit increase and have now added monitoring for `kube_persistentvolumeclaim_status_phase` to detect stuck claims in real-time.

## 2026-04-18 VPA misconfiguration mass pod eviction

<!-- primary: k8s/scaling -->
<!-- secondary: k8s/scheduling -->

During the 2026-04-18 traffic peak, a misconfigured VPA caused a mass eviction of pods by aggressively increasing memory requests beyond node capacity. The `VerticalPodAutoscaler` was set to `updateMode: Auto` without adequate `minAllowed` limits, leading to a scheduling bottleneck. We stabilized the cluster by switching the VPA to `Initial` mode and implemented more conservative `resourcePolicy` constraints in our production templates.
