"""aic setup — One-time AI Core infrastructure setup."""

import json
import os
from pathlib import Path

from aic import ui
from aic.client import (
    create_client,
    get_resource_group,
    save_secrets_dir,
    save_templates_dir,
    templates_dir,
    DEFAULT_AIC_KEY,
    DEFAULT_DOCKER_KEY,
    DEFAULT_S3_KEY,
    DEFAULT_GITHUB_KEY,
    DEFAULT_ARGOCD_KEY,
    secrets_dir,
)
from aic.s3 import load_credentials


def _scan_secrets(directory: Path) -> dict[str, Path]:
    """Scan a directory and classify JSON files by their structure."""
    found = {}
    for f in sorted(directory.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if isinstance(data, dict):
            if "serviceurls" in data and "AI_API_URL" in data.get("serviceurls", {}):
                found.setdefault("aic", f)
            elif ".dockerconfigjson" in data:
                found.setdefault("docker", f)
            elif "application_name" in data and "repository_url" in data:
                found.setdefault("argocd", f)
            elif "url" in data and "username" in data and "password" in data and "name" in data:
                found.setdefault("github", f)
            elif any(k in data for k in ("access_key_id", "aws_access_key_id")):
                found.setdefault("s3", f)
            elif data.get("type") == "service_account" and "project_id" in data:
                found.setdefault("s3", f)  # GCP service account
            elif "account_name" in data and ("account_key" in data or "connection_string" in data):
                found.setdefault("s3", f)  # Azure Blob
    return found


def _configure_credentials() -> Path:
    """Pre-step: locate credentials directory, auto-detect key files, persist config."""
    ui.header("Credentials")
    ui.dim(f"  Current credentials directory: {secrets_dir()}")
    raw = ui.prompt("Path to credentials directory (or directly to aic-key.json)",
                    default=str(secrets_dir()))
    given = Path(raw).expanduser().resolve()

    # If user gave a file path, use its parent as the dir
    if given.is_file():
        key_path = given
        creds_dir = given.parent
    else:
        creds_dir = given
        # Scan for key files
        detected = _scan_secrets(creds_dir)
        if detected:
            print()
            ui.dim("  Auto-detected credential files:")
            labels = {"aic": "AI Core key", "s3": "Object store key", "docker": "Docker secret",
                      "github": "GitHub repo", "argocd": "ArgoCD app"}
            for kind, path in detected.items():
                ui.dim(f"    {labels.get(kind, kind):20s} → {path.name}")
        key_path = detected.get("aic") or creds_dir / "aic-key.json"

    save_secrets_dir(creds_dir)
    ui.success(f"Credentials directory set to: {creds_dir}")

    # Templates directory
    print()
    current_tmpl = templates_dir()
    tmpl_raw = ui.prompt("Path to ServingTemplate directory (app/*.yaml)",
                         default=str(current_tmpl))
    save_templates_dir(Path(tmpl_raw))
    ui.success(f"Templates directory set to: {tmpl_raw}")

    return key_path


def run():
    """Run the 4-step setup flow."""
    aic_key_path = _configure_credentials()
    client = create_client(aic_key_path=aic_key_path)
    rg = get_resource_group()

    ui.banner("aic setup", f"One-time AI Core infrastructure setup (resource group: {rg})")

    step_docker_secret(client)
    step_github_repo(client)
    step_argocd_app(client)
    step_object_store(client, rg)

    print()
    ui.success("All prerequisites ready.")


# ---------------------------------------------------------------------------
# Step 1: Docker Registry Secret
# ---------------------------------------------------------------------------

def step_docker_secret(client):
    ui.header("Step 1/4 — Docker Registry Secret")

    try:
        secrets = client.docker_registry_secrets.query()
        existing = list(secrets.resources) if hasattr(secrets, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query docker secrets: {e}")
        existing = []

    if existing:
        ui.table(
            "Registered Docker Secrets",
            [{"name": "#", "style": "cyan"}, {"name": "Name", "style": "green"}],
            [[str(i), s.name] for i, s in enumerate(existing, 1)],
        )
        ui.success(f"{len(existing)} docker secret(s) found")
        if not ui.confirm("Register another docker secret?", default=False):
            return

    _create_docker_secret(client)


def _create_docker_secret(client):
    print()
    key_path = ui.prompt("Path to Docker secret JSON file", default=str(DEFAULT_DOCKER_KEY()))
    if os.path.exists(key_path):
        with open(key_path) as f:
            data = json.load(f)
        if ".dockerconfigjson" in data:
            config = json.loads(data[".dockerconfigjson"])
            registries = list(config.get("auths", {}).keys())
            name = ui.prompt("Secret name (used in imagePullSecrets)", default=registries[0].split(".")[0] if registries else "registry-secret")
            ui.dim(f"  Registry: {', '.join(registries)}")
            if not ui.confirm(f"Create docker secret '{name}'?"):
                return
            try:
                client.docker_registry_secrets.create(name=name, data=data)
                ui.success(f"Docker secret '{name}' created")
            except Exception as e:
                ui.error(f"Failed to create docker secret: {e}")
            return

    # Fallback: manual entry
    name = ui.prompt("Secret name (used in imagePullSecrets)")
    registry = ui.prompt("Registry URL (e.g. https://registry.example.com)")
    username = ui.prompt("Username")
    password = ui.prompt("Password / Token")

    if not all([name, registry, username, password]):
        ui.error("All fields are required.")
        return

    dockerconfig = json.dumps({"auths": {registry: {"username": username, "password": password}}})

    if not ui.confirm(f"Create docker secret '{name}' for {registry}?"):
        return

    try:
        client.docker_registry_secrets.create(name=name, data={".dockerconfigjson": dockerconfig})
        ui.success(f"Docker secret '{name}' created")
    except Exception as e:
        ui.error(f"Failed to create docker secret: {e}")


# ---------------------------------------------------------------------------
# Step 2: GitHub Repository
# ---------------------------------------------------------------------------

def step_github_repo(client):
    ui.header("Step 2/4 — GitHub Repository (your templates repo)")

    try:
        repos = client.repositories.query()
        existing = list(repos.resources) if hasattr(repos, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query repositories: {e}")
        existing = []

    if existing:
        rows = []
        for i, r in enumerate(existing, 1):
            url = getattr(r, "repository_url", getattr(r, "url", "N/A"))
            rows.append([str(i), r.name, url[:60]])
        ui.table(
            "Registered GitHub Repositories",
            [{"name": "#", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "URL"}],
            rows,
        )
        ui.success(f"{len(existing)} repository(s) found")
        if not ui.confirm("Register another repository?", default=False):
            return

    _create_github_repo(client)


def _create_github_repo(client):
    print()
    key_path = ui.prompt("Path to GitHub repo JSON file", default=str(DEFAULT_GITHUB_KEY()))
    if os.path.exists(key_path):
        with open(key_path) as f:
            data = json.load(f)
        if all(k in data for k in ("name", "url", "username", "password")):
            ui.dim(f"  Name:     {data['name']}")
            ui.dim(f"  URL:      {data['url']}")
            ui.dim(f"  Username: {data['username']}")
            if not ui.confirm(f"Register repo '{data['name']}' → {data['url']}?"):
                return
            try:
                client.repositories.create(
                    name=data["name"], url=data["url"],
                    username=data["username"], password=data["password"],
                )
                ui.success(f"Repository '{data['name']}' registered")
            except Exception as e:
                ui.error(f"Failed to register repository: {e}")
            return
        else:
            ui.warning("JSON file missing required keys (name, url, username, password) — falling back to manual entry")

    # Fallback: manual entry
    ui.dim("  This should be the repo that contains your app/*.yaml ServingTemplates.")
    name = ui.prompt("Repository name")
    url = ui.prompt("Repository URL (e.g. https://github.com/your-org/your-templates-repo)")
    username = ui.prompt("GitHub username")
    password = ui.prompt("GitHub PAT (personal access token)")

    if not all([name, url, username, password]):
        ui.error("All fields are required.")
        return

    if not ui.confirm(f"Register repo '{name}' → {url}?"):
        return

    try:
        client.repositories.create(name=name, url=url, username=username, password=password)
        ui.success(f"Repository '{name}' registered")
    except Exception as e:
        ui.error(f"Failed to register repository: {e}")


# ---------------------------------------------------------------------------
# Step 3: ArgoCD Application
# ---------------------------------------------------------------------------

def step_argocd_app(client):
    ui.header("Step 3/4 — ArgoCD Application (syncs YAML to AI Core)")

    try:
        apps = client.applications.query()
        existing = list(apps.resources) if hasattr(apps, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query applications: {e}")
        existing = []

    if existing:
        rows = []
        for i, app in enumerate(existing, 1):
            try:
                status = client.applications.get_status(application_name=app.application_name)
                sync = f"{getattr(status, 'health_status', '?')} / {getattr(status, 'sync_status', '?')}"
            except Exception:
                sync = "Unknown"
            rows.append([str(i), app.application_name, app.path, sync])
        ui.table(
            "ArgoCD Applications",
            [{"name": "#", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "Path"}, {"name": "Status", "style": "yellow"}],
            rows,
        )
        ui.success(f"{len(existing)} application(s) found")
        if not ui.confirm("Create another application?", default=False):
            return

    _create_argocd_app(client)


def _create_argocd_app(client):
    print()
    key_path = ui.prompt("Path to ArgoCD app JSON file", default=str(DEFAULT_ARGOCD_KEY()))
    if os.path.exists(key_path):
        with open(key_path) as f:
            data = json.load(f)
        if all(k in data for k in ("application_name", "repository_url", "path")):
            revision = data.get("revision", "HEAD")
            ui.dim(f"  App name: {data['application_name']}")
            ui.dim(f"  Repo URL: {data['repository_url']}")
            ui.dim(f"  Path:     {data['path']}")
            ui.dim(f"  Revision: {revision}")
            if not ui.confirm(f"Create app '{data['application_name']}' → {data['repository_url']} / {data['path']}?"):
                return
            try:
                client.applications.create(
                    application_name=data["application_name"],
                    repository_url=data["repository_url"],
                    path=data["path"],
                    revision=revision,
                )
                ui.success(f"Application '{data['application_name']}' created")
            except Exception as e:
                ui.error(f"Failed to create application: {e}")
            return
        else:
            ui.warning("JSON file missing required keys (application_name, repository_url, path) — falling back to manual entry")

    # Fallback: manual entry
    ui.dim("  This points AI Core to the folder in your templates repo that contains app/*.yaml files.")
    name = ui.prompt("Application name")
    repo_url = ui.prompt("Repository URL")
    path = ui.prompt("Path in repo (folder containing ServingTemplate YAMLs)", default="app")
    revision = ui.prompt("Revision", default="HEAD")

    if not all([name, repo_url, path]):
        ui.error("Name, URL, and path are required.")
        return

    if not ui.confirm(f"Create app '{name}' → {repo_url} / {path}?"):
        return

    try:
        client.applications.create(application_name=name, repository_url=repo_url, path=path, revision=revision)
        ui.success(f"Application '{name}' created")
    except Exception as e:
        ui.error(f"Failed to create application: {e}")


# ---------------------------------------------------------------------------
# Step 4: Object Store (S3)
# ---------------------------------------------------------------------------

def step_object_store(client, resource_group: str):
    ui.header("Step 4/4 — Object Store (S3)")

    try:
        secrets = client.object_store_secrets.query()
        existing = list(secrets.resources) if hasattr(secrets, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query object store secrets: {e}")
        existing = []

    if existing:
        rows = []
        for i, s in enumerate(existing, 1):
            rows.append([str(i), s.name, getattr(s, "type", "S3"), getattr(s, "path_prefix", "N/A")])
        ui.table(
            "Object Store Secrets",
            [{"name": "#", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "Type"}, {"name": "Path Prefix"}],
            rows,
        )
        ui.success(f"{len(existing)} object store(s) found")
        if not ui.confirm("Register another object store?", default=False):
            return

    _create_object_store(client, resource_group)


def _create_object_store(client, resource_group: str):
    print()
    key_path = ui.prompt("Path to S3 credentials JSON file", default=str(DEFAULT_S3_KEY()))
    if not os.path.exists(key_path):
        ui.error(f"File not found: {key_path}")
        return

    try:
        creds = load_credentials(key_path)
    except (json.JSONDecodeError, ValueError) as e:
        ui.error(f"Invalid credentials file: {e}")
        return

    name = ui.prompt("Object store secret name (used in artifact URLs: ai://<name>/...)")
    path_prefix = ui.prompt("S3 path prefix (folder where models are stored, e.g. my-project/models)")

    ui.dim(f"\n  Bucket:   {creds['bucket']}")
    ui.dim(f"  Endpoint: {creds.get('host', 'default')}")
    ui.dim(f"  Region:   {creds.get('region', 'eu-central-1')}")
    ui.dim(f"  Prefix:   {path_prefix}")
    ui.dim(f"  Artifact URLs will resolve: ai://{name}/<model> → s3://{creds['bucket']}/{path_prefix}/<model>/")

    if not ui.confirm(f"\nCreate object store secret '{name}'?"):
        return

    try:
        client.object_store_secrets.create(
            name=name,
            type="S3",
            resource_group=resource_group,
            path_prefix=path_prefix,
            region=creds.get("region", "eu-central-1"),
            bucket=creds["bucket"],
            endpoint=creds.get("host", "s3.amazonaws.com"),
            data={
                "AWS_ACCESS_KEY_ID": creds["access_key_id"],
                "AWS_SECRET_ACCESS_KEY": creds["secret_access_key"],
            },
        )
        ui.success(f"Object store secret '{name}' created")
    except Exception as e:
        ui.error(f"Failed to create object store secret: {e}")
