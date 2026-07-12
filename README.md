# entryplug-base

Base Kubernetes manifests for my personal servers. Used by separate ArgoCD repo via Kustomize remote bases - cluster-specific patches and values are all there.

## Folder Structure

```
manifests/
  apps/           # Application workloads
  infra/
    custom/       # Custom infra resources (gateway, certs, MetalLB, ESO store, secrets)
    external/     # External Kustomize manifest installs (cert-manager, envoy-gateway, ESO, metallb)
  rauthy/         # Rauthy IAM provider
```
