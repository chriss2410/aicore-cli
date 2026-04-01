# aicore-cli

A CLI tool (`aic`) for deploying custom models on [SAP AI Core](https://help.sap.com/docs/sap-ai-core). It replaces the manual process of notebooks, scripts, and YAML editing and using SAP AI Launchpad with two commands: `aic setup` and `aic deploy`.

## Two repositories, one workflow

Using `aic` requires **two separate repositories**:

```
aicore-cli/                  ← THIS repo — install once, never push secrets
│   aic/                     ← CLI source code
│   secrets/                 ← YOUR credentials (gitignored — never committed)
│       aic-key.json
│       s3-key.json
│       docker-secret.json
│       github-key.json
│       argocd-key.json
│
your-mlops-repo/             ← YOUR project repo — one per AI Core project
    app/*.yaml               ← ServingTemplates (synced to AI Core via ArgoCD)
    src/                     ← Dockerfiles, inference server code
    examples/                ← Test scripts, example configs
    deployment_config.yaml   ← Deployment parameters for aic deploy
    models/                  ← Local model weights for S3 upload (gitignored)
    logs/                    ← Written by aic at runtime (gitignored)
```

**`aicore-cli`** — You install this once on your machine. It provides the `aic` command globally. Your credential files (`secrets/`) live here and are gitignored, so they never leave your machine. You run `aic` from within your MLOps repo.

**Your MLOps repo** — One per AI Core project. Contains your ServingTemplate YAMLs in `app/` (synced to AI Core via ArgoCD), your Dockerfiles and inference code, and a `deployment_config.yaml`. This repo is what you push to GitHub and connect to AI Core.

## Installation

```bash
git clone https://github.com/your-user/aicore-cli
cd aicore-cli
uv sync
```
(alternatively `pip install -e .`)
After installation, the `aic` command is available in your shell.

---

## How SAP AI Core deployments work

### Concepts

**ServingTemplate** — A YAML file that defines how AI Core should run your model: which container image, what GPU instance, which parameters are configurable. ServingTemplates live in `app/` and are synced to AI Core via **ArgoCD**.

**Scenario & Executable** — A ServingTemplate registers a *scenario* (a logical grouping) and an *executable* (the serving spec). These identifiers are referenced in your deployment config.

**Configuration** — A binding of concrete parameter values to an executable. AI Core uses it to create a KServe InferenceService.

**Artifact** — A pointer to a model on S3. When a deployment starts, AI Core downloads the artifact and mounts it into the container at `/mnt/models`.

**Object Store** — An S3-backed BTP service whose credentials you register with AI Core. Lets AI Core resolve `ai://<store>/<model>` artifact URLs to actual S3 paths.

### Deployment flow

```
your-templates-repo (app/*.yaml)
        │
        ▼ ArgoCD sync
   AI Core registers Scenario + Executable
        │
        ▼ aic deploy
   Upload model → S3
   Build & push container image
   Create Artifact  (ai://store/model → S3 path)
   Create Configuration  (parameter values)
   Create Deployment → KServe InferenceService starts
```

For a deeper introduction, see the [SAP AI Core documentation](https://help.sap.com/docs/sap-ai-core).

---

## Usage

> Run all `aic` commands from inside your MLOps repo (the one with `app/*.yaml`), not from the `aicore-cli` directory.

```bash
# One-time infrastructure setup
aic setup
aic setup --auto

# Deploy a model
aic deploy                                        # interactive
aic deploy --auto --config deployment_config.yaml

# Manage deployments
aic deploy --list
aic deploy --status <ID>
aic deploy --logs <ID>
aic deploy --stop <ID>
aic deploy --update <ID>
aic deploy --list-artifacts
aic deploy --list-configs
```

Run `aic` from your MLOps repo. Logs are written to `./logs/` in your working directory.

---

## Credentials

Credentials live in **`secrets/`** inside the `aicore-cli` directory (gitignored, never committed). The five key files below are expected there. Run `aic setup` once to populate them interactively.

> Alternatively, place credentials anywhere and set the `AIC_SECRETS_DIR` environment variable, or use `~/.aic/` as a global fallback — `aic setup` saves the location automatically so all subsequent commands find the right files.

### 1. AI Core Service Key — `aic-key.json`

Download from **BTP Cockpit → AI Core instance → Service Keys**:

```json
{
  "serviceurls": {
    "AI_API_URL": "https://api.ai.intprod-eu12.eu-central-1.aws.ml.hana.ondemand.com"
  },
  "appname": "...",
  "clientid": "sb-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx!bNNNNNN|xsuaa_std!bNNNNNN",
  "clientsecret": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx$xxxxxxxxxxxxxxxxxxxxxxxxx=",
  "identityzone": "your-subaccount",
  "identityzoneid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "url": "https://your-subaccount.authentication.eu12.hana.ondemand.com",
  "credential-type": "binding-secret"
}
```

### 2. S3 Object Store Key — `s3-key.json`

Download from **BTP Cockpit → Object Store instance → Service Keys**:

```json
{
  "access_key_id": "AKIA...",
  "secret_access_key": "...",
  "bucket": "hcp-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "host": "s3-eu-central-1.amazonaws.com",
  "region": "eu-central-1",
  "uri": "s3://AKIA...@s3-eu-central-1.amazonaws.com/hcp-...",
  "username": "hcp-s3-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

When AI Core resolves `ai://my-store/MyModel`, it maps this to `s3://<bucket>/<path_prefix>/MyModel/` and mounts the contents at `/mnt/models` in the container.

### 3. Docker Registry Secret — `docker-secret.json`

Create manually or let `aic setup` generate it interactively:

```json
{
  ".dockerconfigjson": "{\"auths\":{\"your-registry.example.com\":{\"username\":\"your-user\",\"password\":\"your-token\"}}}"
}
```

The secret name you choose here must match `imagePullSecrets` in your ServingTemplate.

### 4. GitHub Repository — `github-key.json`

Credentials for the templates repo AI Core pulls ServingTemplates from. Requires a personal access token with repo read access:

```json
{
  "name": "my-templates-repo",
  "url": "https://github.com/your-org/your-templates-repo",
  "username": "your-username",
  "password": "<YOUR GITHUB TOKEN>"
}
```

### 5. ArgoCD Application — `argocd-key.json`

Defines the ArgoCD application that watches `path` for ServingTemplate YAMLs:

```json
{
  "application_name": "my-argo-app",
  "repository_url": "https://github.com/your-org/your-templates-repo",
  "path": "app",
  "revision": "HEAD"
}
```

---

## Deployment configuration
A file that allows you to simply write all aspects of one deployment into a single file, easily debuggable and updateable with AI.
By convention the file is named `deployment_config.yaml` and lives in the repo root, but you can point `--config` at any path.  
Of course you need to adjust it according to your setup

```yaml
general:
  name: "My Deployment"
  scenario_id: "my-scenario"           # Must match scenario label in your ServingTemplate
  resource_group: "default"
  executable_id: "my-serving-template" # Must match the ServingTemplate name

model:
  object_store: "my-store"             # AI Core object store secret name
  s3_key_file: "~/.aic/s3-key.json"
  s3_prefix: "my-project/models"       # S3 folder where models are stored
  local_path: ""                       # Set to a local folder path to upload on deploy

docker:
  secret: "my-registry-secret"         # Docker pull secret name registered in AI Core
  image: "my-registry.example.com/my-image:1.0"
  dockerfile: "src/my.dockerfile"      # Path to Dockerfile (relative to repo root)

parameter_bindings:
  - key: "instanceType"
    value: "g6e.4xlarge"
  - key: "minReplicas"
    value: "0"
  - key: "maxReplicas"
    value: "1"
  - key: "window"
    value: "30m"
  - key: "scaleToZeroPodRetentionPeriod"
    value: "60m"
  # ... all parameters defined in your ServingTemplate

# Uncomment to reuse an existing model artifact (skips S3 step)
# input_artifact_bindings:
#   - key: "modeluri"
#     artifact_id: <your-artifact-id>

# Uncomment after first deployment to update instead of creating new
# deployment_id: d4a37cd0-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### Common parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `instanceType` | GPU instance — see table below | `g6e.4xlarge` |
| `containerImage` | Full image tag pushed to your registry | `my-registry.example.com/server:1.0` |
| `portNumber` | Port the container listens on | `8000` |
| `minReplicas` / `maxReplicas` | Knative autoscaling range. Set `minReplicas: 0` for scale-to-zero | `0` / `1` |
| `window` | Knative autoscaler averaging window. Shorter values react faster to traffic but may cause flapping; longer values smooth bursts. Maps to `autoscaling.knative.dev/window`. | `30m` |
| `scaleToZeroPodRetentionPeriod` | How long to keep a pod alive after the last request before scaling to zero. Avoids cold starts for bursty traffic. Only relevant when `minReplicas: 0`. | `60m` |

> **Note:** Parameter keys must exactly match the parameter names defined in your ServingTemplate. Defaults are always read from the ServingTemplate YAML in `app/`.

---

## GPU instance types

![AI Core GPU pricing](https://raw.githubusercontent.com/your-user/aicore-cli/main/img/AI%20Core%20GPU%20pricing.png)

| Instance Type | Cloud | CPU | RAM | GPU | Typical use case |
|---------------|-------|-----|-----|-----|-----------------|
| `g6e.4xlarge` | AWS | 16 cores | 128 GB | 1× L40S (48 GB) | Small models (2B–7B) |
| `a2-ultragpu-1g` | GCP | 12+ cores | 170 GB | 1× A100 (80 GB) | Medium models (7B–13B) |
| `Standard_NC40ads_H100_v5` | Azure | 40 cores | 320 GB | 1× H100 (80 GB) | Large models (13B+) |

Full list: [SAP AI Core — Choose an Instance Type](https://help.sap.com/docs/sap-ai-core/predictive-ai-db13d59d17204c01b3b79c24fb82a19a/choose-instance-abd672fa709b430080ffe76a99f06cee)

---

## Container image requirements

AI Core runs your container on managed GPU infrastructure. The image must:

1. **Run as non-root** — create any required directories (e.g. `/nonexistent`) and set permissions in the Dockerfile
2. **Listen on the configured port** — set via the `portNumber` parameter
3. **Read all config from environment variables** — the ServingTemplate injects every parameter as an env var
4. **Expect the model at `/mnt/models`** — AI Core mounts the S3 artifact there at startup

Build for `linux/amd64` and push to a registry accessible from AI Core. `aic deploy` handles the build and push interactively.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
