# entryplug-base

Base Kubernetes manifests and Helm charts for my personal servers. Used by separate ArgoCD repo via Kustomize remote bases - cluster-specific patches and values are all there.

## Folder Structure

```
manifests/
  apps/           # Application workloads
  infra/
    custom/       # Custom infra resources (gateway, certs, MetalLB, ESO store, secrets)
    helm/         # Helm chart installs (cert-manager, cnpg, envoy-gateway, ESO, metallb)
  rauthy/         # Rauthy IAM provider (Helm chart)
scripts/
  template_config_toml.py   # Generates config.toml.template + values.yaml from config.toml
```


## Rauthy config

`manifests/rauthy/config.toml` is the source of truth for the Rauthy configuration schema. If updating it is required, regenerate the derived files:

```bash
python3 scripts/template_config_toml.py --chart-dir manifests/rauthy
```

This updates `config.toml.template` (used by the Helm chart) and `values.yaml`.
