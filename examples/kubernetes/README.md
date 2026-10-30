# Reolink Monitor on Kubernetes

This Kustomize sample runs the published image with a ConfigMap generated from
`config.yaml`, a pre-created Kubernetes Secret for credentials, and fixed UDP
ports `44230` and `44231` exposed through a LoadBalancer Service.

## Configure

1. Edit `config.yaml` with your MQTT broker and camera addresses. Explicitly
   set `udp_local_port` for every UDP camera; treat it as mandatory for this
   deployment rather than relying on the application default. Each camera
   needs a unique port, and each value must match the Service port and target
   port. The sample uses `44230` and `44231`.
2. Create the credentials Secret in the same namespace where you will deploy
   the monitor. The camera credentials below are shared by both sample cameras:

   ```sh
   kubectl create secret generic reolink-monitor-credentials \
     --from-literal=MQTT_USERNAME="${MQTT_USERNAME:?Set MQTT_USERNAME first}" \
     --from-literal=MQTT_PASSWORD="${MQTT_PASSWORD:?Set MQTT_PASSWORD first}" \
     --from-literal=CAMERA_USERNAME="${CAMERA_USERNAME:?Set CAMERA_USERNAME first}" \
     --from-literal=CAMERA_PASSWORD="${CAMERA_PASSWORD:?Set CAMERA_PASSWORD first}" \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

   Export those four variables in your shell before running the command. The
   secret values are not stored in this directory or in the generated
   ConfigMap.

## Deploy

From this directory:

```sh
kubectl apply -k .
```

Kustomize builds a ConfigMap from `config.yaml`, appends a content hash to its
name, and updates the Deployment's volume reference automatically. When the
configuration changes, re-applying the sample rolls the Deployment to use the
new ConfigMap.

The `reolink-monitor-udp` LoadBalancer Service exposes UDP ports `44230` and
`44231`, with each Service port forwarded to the identical container port.
The cluster's load balancer must support UDP and allow these ports through its
firewall. `externalTrafficPolicy: Local` avoids Service-side source-IP
masquerading for ingress traffic, but does not guarantee that the cluster
network or outbound NAT preserves the Baichuan UDP ports.

Baichuan is very sensitive to UDP port changes: the local UDP port is sent as
protocol metadata and must remain unchanged end to end. Do not translate it
with NAT or port mapping, including translation of the source port. A 1:1
Service port mapping alone cannot guarantee this; verify the cluster's CNI,
load balancer, and egress path preserve ports in both directions. Docker
Desktop environments can be especially challenging because their VM/network
NAT may rewrite UDP ports. Test against the cameras from the actual cluster
before relying on this deployment.

Run a single replica: the cameras' protocol state belongs to the process bound
to those ports.

Check rollout and logs with:

```sh
kubectl rollout status deployment/reolink-monitor
kubectl logs deployment/reolink-monitor
```
