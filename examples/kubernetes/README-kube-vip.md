
# kube-vip egress on k3s: keep a UDP source port unchanged

**Goal:** a pod that initiates UDP connections leaves the node with the kube-vip VIP as source address and its original source port. By default, flannel's masquerade rewrites the port because it uses `--random-fully`.

**Setup:** single-node k3s with flannel, kube-vip in ARP mode (DaemonSet), host on the `nf_tables` iptables backend. The kube-vip image has no `iptables` binary, so the built-in nftables implementation (`egress-internal`) is used.

## 1. kube-vip DaemonSet

Add these to the `env:` list of the kube-vip container, next to your existing `svc_enable: "true"`:

```yaml
- name: svc_election
  value: "true"
- name: egress_podcidr
  value: "10.42.0.0/16"        # your k3s cluster-cidr (IPv4 part)
- name: egress_servicecidr
  value: "10.43.0.0/16"        # your k3s service-cidr (IPv4 part)
```

- `svc_election` gives each Service its own leader lease. Egress depends on it. In the logs you'll see "leaderelection will happen for every service".
- The two CIDRs exclude pod-to-pod and pod-to-ClusterIP traffic from SNAT. kube-vip's defaults (`10.0.0.0/16`, `10.96.0.0/12`) don't match k3s.
- Edit the file or Helm values that actually manage the DaemonSet, otherwise your change is reverted. With `data-dir: /data/k3s`, auto-applied manifests live in `/data/k3s/server/manifests/`.

## 2. Kernel modules on the node

kube-vip's nftables egress needs `nft_ct` and `nft_queue`. Without them it logs `missing nftables modules` and creates no rule.

```bash
sudo modprobe nft_ct nft_queue
printf 'nft_ct\nnft_queue\n' | sudo tee /etc/modules-load.d/kube-vip-nft.conf
```

The second command makes the modules load at every boot.

## 3. The Service

The egress settings must be **annotations**, not labels. kube-vip ignores labels.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: reolink-monitor-udp
  namespace: reolink
  annotations:
    kube-vip.io/loadbalancerIPs: 192.168.2.195
    kube-vip.io/egress: "true"
    kube-vip.io/egress-internal: "true"      # nftables implementation, no iptables binary needed
spec:
  type: LoadBalancer
  externalTrafficPolicy: Local               # required for egress
  loadBalancerIP: 192.168.2.195
  selector:
    app.kubernetes.io/name: reolink-monitor  # the pod must be an endpoint of this Service
  ports:
    - name: camera-udp-1
      port: 44230
      protocol: UDP
      targetPort: 44230
    - name: camera-udp-2
      port: 44231
      protocol: UDP
      targetPort: 44231
```

After applying, kube-vip logs `egress configuration updated successfully` for the Service. Restart the application pod afterwards so it opens fresh flows, because conntrack keeps the NAT mapping of existing UDP flows. The kube-vip docs also describe a `kube-vip.io/flush-conntrack` annotation for this.

## 4. What a working setup looks like

`sudo nft list ruleset` should now contain a `kube_vip_v4` table:

```
table ip kube_vip_v4 {
	chain kube_vip_snat_bf8df616-a5f6-4707-840f-6eabdb62ee91 {
		type nat hook postrouting priority srcnat; policy accept;
		ip saddr { 10.42.0.47 } ip daddr != 10.42.0.0-10.42.255.255 ip daddr != 10.43.0.0-10.43.255.255 snat to 192.168.2.195
	}
}
```

How to read it:
- **One chain per egress Service**, named with the Service's UID.
- **`type nat hook postrouting priority srcnat`**: it runs in the same stage and at the same priority as the iptables-nft NAT chains from k3s and flannel. See the ordering tip below.
- **`ip saddr { 10.42.0.47 }`** is the pod's current IP. kube-vip updates it when the pod is replaced.
- **The two `ip daddr !=` terms** are your `egress_podcidr` and `egress_servicecidr`. Traffic to those ranges is not SNAT'd.
- **`snat to 192.168.2.195`** has no port range, so conntrack keeps the source port unless two flows would collide on the same tuple. This is the behavior you want. Flannel's rule uses `--random-fully`, which always randomizes.
- This SNATs all of the pod's traffic to destinations outside the cluster, not just the UDP flows. You can narrow it with `kube-vip.io/egress-destination-ports` (format `udp:5060`, comma-separated). The services docs also list `egress-source-ports`, `egress-allowed-networks` and `egress-denied-networks`, but I couldn't confirm their exact semantics, so test them before relying on them.

**Final verification:**
```bash
sudo tcpdump -ni ens9 udp and host <peer-ip>
```
The source should be `192.168.2.195` with the same port the app bound to.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No `kube_vip_v4` table, nothing about egress in the logs | The keys are in `labels:` instead of `annotations:`. Check with `kubectl -n <ns> get svc <name> -o jsonpath='{.metadata.annotations}'`. |
| Log says `missing nftables modules -> nft_ct ... nft_queue` | Load the modules (step 2), then re-sync: `kubectl -n kube-system rollout restart ds/kube-vip-ds`, or re-annotate or recreate the Service. The egress setup only runs when the Service is synchronised. |
| `exec: "iptables": executable file not found` | The plain image has no iptables. Use `kube-vip.io/egress-internal: "true"`, or the iptables image variant with `egress_withnftables` (check the kube-vip releases for the tag). |
| Rule exists but the tcpdump still shows the node IP | Check that the pod is an endpoint (`kubectl get endpointslices`), that the pod IP in the rule matches `kubectl get pod -o wide`, and that you restarted the app so it isn't reusing an old conntrack entry. List entries with `conntrack -L -p udp`, delete with `conntrack -D -p udp`. |
| Source IP is correct but the port still changes | Possible collision (another flow with the same source, destination and port) or a stale conntrack entry. Also compare chain order: `sudo nft -a list ruleset` shows the priorities. kube-vip's chain and the iptables-nft chains share priority `srcnat`, so don't assume the order. Confirm with tcpdump. |
| Pod-to-pod or DNS traffic breaks | Egress CIDR variables don't match your cluster ranges. Check `egress_podcidr` and `egress_servicecidr` against `kubectl get node -o jsonpath='{.items[*].spec.podCIDR}'` and `kubectl get svc kubernetes`. |
| Works until reboot, then fails | The modules aren't loading at boot. Check `/etc/modules-load.d/` and `systemctl status systemd-modules-load`. |
| Egress works but the peer rejects traffic | The peer or its firewall must accept the VIP `192.168.2.195` as the source address, not the node IP. |
| Log shows `vip=10.43.0.0/16` in the `[Egress]` line | Looks like a cosmetic log quirk. The real check is the `snat to` address in the nft rule. |

Useful commands:
```bash
kubectl -n kube-system logs ds/kube-vip-ds --since=5m | grep -i -e egress -e nft -e snat -e error
kubectl -n kube-system get ds kube-vip-ds -o jsonpath='{.spec.template.spec.containers[0].image}'
sudo nft list tables
sudo nft list ruleset | grep -n -i -B3 -A12 kube_vip
```

To roll back, remove the two egress annotations. kube-vip deletes the chain and the pod goes back to flannel's masquerade.

## Not covered: IPv6

I haven't tested IPv6 egress. The relevant annotation is `kube-vip.io/egress-ipv6`, and your global IPv6 VIPs already work for ordinary Services. I couldn't confirm how the CIDR exclusion variables handle IPv6 ranges (yours are `fdd7:c7e6:fad4:301::/64` for pods and `fdd7:c7e6:fad4:f301::/110` for services). Test that on a throwaway Service first, and check `nft list ruleset` for the exclusions.

I can also turn this into a standalone doc, or add an IPv6 section once you've tried it.