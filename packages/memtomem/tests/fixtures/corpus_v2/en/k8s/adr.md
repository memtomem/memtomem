> Synthetic content for search regression testing — verify before adopting as runbook.

## KEDA over HPA for event-driven Kafka consumer scaling

<!-- primary: k8s/scaling -->
<!-- secondary: observability/metrics, cost_optimization/compute -->

We chose KEDA over standard HorizontalPodAutoscaler to handle event-driven scaling for our Kafka consumers. By targeting the `kafka_consumergroup_lag` metric instead of CPU, we can scale to zero replicas during idle periods, significantly reducing `cost_optimization/compute` costs. The accepted trade-off is the added complexity of managing the ScaledObject CRD and its integration with our Prometheus instance.

## WaitForFirstConsumer mode to prevent cross-zone PV conflicts

<!-- primary: k8s/storage -->
<!-- secondary: k8s/scheduling -->

We decided to implement `volumeBindingMode: WaitForFirstConsumer` in our Production `StorageClass` definitions. This ensures that PersistentVolumes are only provisioned in the specific Availability Zone where the scheduler places the Pod, preventing 'node affinity conflict' errors. While this increases initial Pod startup latency slightly, it eliminates the need for manual volume migration across zones.

## NetworkPolicy over Service Mesh for microservice isolation

<!-- primary: k8s/networking -->
<!-- secondary: security/access_control -->

The team adopted `NetworkPolicy` as the primary mechanism for microservice isolation instead of a full Service Mesh. We apply a 'default-deny-all' ingress policy in the `prod` namespace to enforce zero-trust networking. This approach minimizes the resource overhead on our `t3.medium` worker nodes compared to running sidecar proxies for every workload.

## system-cluster-critical priority for core monitoring agents

<!-- primary: k8s/scheduling -->
<!-- secondary: k8s/scaling -->

We prioritized `priorityClassName: system-cluster-critical` for our core monitoring agents to prevent eviction during high node pressure. This ensures that Prometheus and Fluent-bit remain operational even when the `cluster-autoscaler` is struggling to provision new nodes. We accept that lower-priority batch jobs may be preempted more frequently during peak traffic spikes.
