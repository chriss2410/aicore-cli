"""aic deploy — Full deployment pipeline + management commands."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from ai_api_client_sdk.models.artifact import Artifact
from ai_api_client_sdk.models.input_artifact_binding import InputArtifactBinding
from ai_api_client_sdk.models.parameter_binding import ParameterBinding
from ai_core_sdk.models import TargetStatus

from aic import ui
from aic.client import create_client, get_resource_group, templates_dir, save_templates_dir
from aic import s3 as s3ops
from aic import docker

CONFIG_PATH = "deployment_config.yaml"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_defaults_from_template(yaml_path: str) -> dict:
    """Parse a ServingTemplate YAML from app/ and return a config dict populated with its defaults."""
    with open(yaml_path) as f:
        tmpl = yaml.safe_load(f)

    meta = tmpl.get("metadata", {})
    labels = meta.get("labels", {})
    parameters = tmpl.get("spec", {}).get("inputs", {}).get("parameters", [])

    scenario_id = labels.get("scenarios.ai.sap.com/id", "")
    executable_id = meta.get("name", "")

    param_bindings = [
        {"key": p["name"], "value": str(p["default"])}
        for p in parameters
        if "default" in p
    ]
    container_image = next((p["value"] for p in param_bindings if p["key"] == "containerImage"), "")

    return {
        "general": {
            "name": scenario_id,
            "scenario_id": scenario_id,
            "resource_group": "default",
            "executable_id": executable_id,
        },
        "model": {
            "object_store": "",
            "s3_key_file": str(s3ops.DEFAULT_S3_KEY()),
            "s3_prefix": s3ops.DEFAULT_PREFIX,
            "local_path": "",
        },
        "docker": {
            "secret": "",
            "image": container_image,
            "dockerfile": "",
        },
        "parameter_bindings": param_bindings,
    }


def pick_input_mode() -> dict:
    """Ask how to load deployment config. Returns a config dict."""
    print("\nHow do you want to configure this deployment?\n")
    print("  [1] Interactive — pick a scenario template from app/ and enter values")
    print("  [2] Config file — provide path to a deployment_config.yaml")

    mode = input("\nSelect (1-2) [1]: ").strip() or "1"

    if mode == "2":
        path = input("  Config file path: ").strip()
        if not os.path.exists(path):
            ui.error(f"File not found: {path}")
            sys.exit(1)
        return load_config(path)

    # Mode 1: pick from configured templates directory
    app_dir = templates_dir()
    if not app_dir.is_dir():
        print()
        ui.warning(f"Templates directory not found: {app_dir}")
        raw = ui.prompt("Enter path to ServingTemplate directory")
        app_dir = Path(raw).expanduser().resolve()
        if app_dir.is_dir():
            save_templates_dir(app_dir)
            ui.success(f"Saved: will use {app_dir} for future deploys")
    templates = sorted(app_dir.glob("*.yaml")) if app_dir.is_dir() else []
    if not templates:
        ui.error("No YAML files found in app/")
        sys.exit(1)

    print("\nAvailable scenario templates:\n")
    for i, t in enumerate(templates, 1):
        print(f"  [{i}] {t.name}")

    raw = input(f"\nSelect template (1-{len(templates)}) [1]: ").strip() or "1"
    try:
        idx = int(raw) - 1
        if not 0 <= idx < len(templates):
            raise ValueError
    except ValueError:
        ui.error("Invalid selection")
        sys.exit(1)

    return load_defaults_from_template(str(templates[idx]))


# ============================================================================
# Main deploy flow
# ============================================================================

def run(config_path: str = None, scenario_id: str = None):
    """Run the deploy pipeline."""
    auto = bool(config_path)
    if auto:
        if not config_path or not os.path.exists(config_path):
            ui.error("--auto requires --config <path> pointing to an existing deployment_config.yaml")
            return
        config = load_config(config_path)
    else:
        config = pick_input_mode()
    client = create_client()
    rg = get_resource_group()

    ui.banner("aic deploy", f"Deploy model to AI Core (resource group: {rg})")

    # Resolve settings from config
    model_cfg = config.get("model", {})
    docker_cfg = config.get("docker", {})
    general = config.get("general", {})
    params = {p["key"]: p["value"] for p in config.get("parameter_bindings", [])}

    object_store = model_cfg.get("object_store", "")
    s3_key_file = model_cfg.get("s3_key_file", str(s3ops.DEFAULT_S3_KEY()))
    s3_prefix = model_cfg.get("s3_prefix", s3ops.DEFAULT_PREFIX)
    local_model_path = model_cfg.get("local_path", "")
    container_image = docker_cfg.get("image", params.get("containerImage", ""))
    dockerfile = docker_cfg.get("dockerfile", "")
    # --scenario flag overrides config
    if scenario_id:
        general["scenario_id"] = scenario_id
    scenario_id = general.get("scenario_id", "")

    # Step 1: Model artifact (select existing, or upload to S3 + register new)
    artifact_id, model_folder = step_model_artifact_combined(
        client, object_store, s3_key_file, s3_prefix, local_model_path, scenario_id, config, auto
    )

    # Step 2: Container image
    container_image = step_container_image(container_image, dockerfile, auto)
    params["containerImage"] = container_image

    # Step 3: Validate ArgoCD sync + scenario exists
    scenario_id = step_validate_scenario(client, scenario_id, auto)
    if not scenario_id:
        ui.error("No valid scenario — cannot continue. Run 'aic setup' to configure ArgoCD.")
        return
    general["scenario_id"] = scenario_id

    # Step 4: Configuration (also selects executable for the chosen scenario)
    config_id = step_configuration(client, general, params, artifact_id, auto, model_folder=model_folder)

    # Step 5: Deploy
    def _save_fn(deployment_id):
        _offer_save_config(
            general=general,
            params=params,
            artifact_id=artifact_id,
            model_cfg={
                "object_store": object_store,
                "s3_key_file": s3_key_file,
                "s3_prefix": s3_prefix,
                "local_path": local_model_path,
            },
            docker_cfg={
                "secret": docker_cfg.get("secret", ""),
                "image": container_image,
                "dockerfile": dockerfile,
            },
            deployment_id=deployment_id,
        )

    step_deploy(client, config_id, config, auto, save_config_fn=None if auto else _save_fn)


# ============================================================================
# Step 1: Model Artifact (select existing OR upload to S3 + register new)
# ============================================================================

def step_model_artifact_combined(
    client, object_store: str, s3_key_file: str, s3_prefix: str,
    local_model_path: str, scenario_id: str, config: dict, auto: bool
) -> tuple[str, str]:
    """Select or create a model artifact. Returns (artifact_id, model_folder)."""
    ui.header("Step 1/5 — Model Artifact")

    # Check if artifact_id already pinned in config
    artifact_bindings = config.get("input_artifact_bindings", [])
    if artifact_bindings:
        existing_id = artifact_bindings[0].get("artifact_id", "")
        if existing_id:
            ui.success(f"Using artifact from config: {existing_id}")
            return existing_id, ""

    # Query registered artifacts in AI Core
    try:
        if scenario_id:
            result = client.artifact.query(scenario_id=scenario_id, kind=Artifact.Kind.MODEL)
        else:
            result = client.artifact.query(kind=Artifact.Kind.MODEL)
        artifacts = list(result.resources) if hasattr(result, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query artifacts: {e}")
        artifacts = []

    if artifacts:
        rows = []
        for i, a in enumerate(artifacts, 1):
            rows.append([str(i), a.name, a.url, a.id[:16] + "..."])
        ui.table(
            "Registered Model Artifacts",
            [{"name": "#", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "URL"}, {"name": "ID", "style": "dim"}],
            rows,
        )

    if auto:
        # Reuse artifact matching model_folder, or create new
        model_folder = local_model_path and os.path.basename(local_model_path.rstrip("/")) or ""
        for a in artifacts:
            if model_folder and model_folder in a.url:
                ui.success(f"Reusing artifact: {a.name} ({a.id})")
                return a.id, model_folder
        if model_folder and scenario_id:
            # Upload to S3 first if local path set
            _auto_upload_if_needed(s3_key_file, s3_prefix, local_model_path, model_folder)
            artifact_id = _create_artifact(client, object_store, model_folder, scenario_id, auto=True)
            return artifact_id, model_folder
        ui.warning("Cannot auto-create artifact — missing model folder or scenario ID")
        return "", ""

    # Interactive: show existing artifacts + option to register new (with optional S3 upload)
    print()
    choices = [f"Use existing: {a.name}  ({a.url})" for a in artifacts]
    choices.append("Register new artifact (upload model to S3 first if needed)")
    for i, c in enumerate(choices, 1):
        print(f"  [{i}] {c}")

    default = "1" if artifacts else str(len(choices))
    choice = ui.prompt(f"Select (1-{len(choices)})", default=default)
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = 0

    if 0 <= idx < len(artifacts):
        selected = artifacts[idx]
        ui.success(f"Selected: {selected.name} ({selected.url})")
        return selected.id, ""

    # Register new: optionally upload to S3 first
    model_folder = _interactive_upload_if_needed(s3_key_file, s3_prefix, local_model_path)
    artifact_id = _create_artifact(client, object_store, model_folder, scenario_id)
    return artifact_id, model_folder


def _auto_upload_if_needed(s3_key_file: str, s3_prefix: str, local_path: str, folder_name: str):
    if not local_path or not os.path.isdir(local_path):
        return
    if not os.path.exists(s3_key_file):
        ui.warning(f"S3 key not found at {s3_key_file} — skipping upload")
        return
    creds = s3ops.load_credentials(s3_key_file)
    s3_client = s3ops.create_client(creds)
    bucket = creds["bucket"]
    full_prefix = f"{s3_prefix}/{folder_name}" if s3_prefix else folder_name
    if not s3ops.path_exists(s3_client, bucket, full_prefix):
        ui.dim(f"  Uploading {local_path} → s3://{bucket}/{full_prefix}/")
        count = s3ops.upload_folder(s3_client, bucket, local_path, full_prefix)
        ui.success(f"Uploaded {count} files")
    else:
        ui.success(f"Model already on S3: {full_prefix}")


def _interactive_upload_if_needed(s3_key_file: str, s3_prefix: str, local_model_path: str) -> str:
    """Optionally upload a model to S3, then return the folder name to use for the artifact URL."""
    print()
    print("  Do you need to upload a model to S3 first?")
    print("  [1] No — model already on S3, just register the artifact")
    print("  [2] Yes — upload a local folder to S3 now")
    choice = ui.prompt("Select (1-2)", default="1")

    if choice == "2":
        if not os.path.exists(s3_key_file):
            ui.error(f"S3 key file not found: {s3_key_file}")
            return ui.prompt("S3 folder name (used in artifact URL)")

        creds = s3ops.load_credentials(s3_key_file)
        s3_client = s3ops.create_client(creds)
        bucket = creds["bucket"]

        # Show existing folders
        folders = s3ops.list_folders(s3_client, bucket, s3_prefix)
        if folders:
            ui.table(
                f"Existing folders on S3 ({s3_prefix}/)",
                [{"name": "#", "style": "cyan"}, {"name": "Folder", "style": "green"}, {"name": "Files"}, {"name": "Size"}],
                _model_folder_rows(s3_client, bucket, s3_prefix, folders),
            )

        local_path = ui.prompt("Local model folder path", default=local_model_path or "")
        if not local_path or not os.path.isdir(os.path.expanduser(local_path)):
            ui.error(f"Not a valid directory: {local_path}")
            return ui.prompt("S3 folder name (used in artifact URL)")

        local_path = os.path.expanduser(os.path.abspath(local_path))
        folder_name = os.path.basename(local_path)
        default_dest = f"{s3_prefix}/{folder_name}" if s3_prefix else folder_name
        dest = ui.prompt("S3 destination path", default=default_dest)

        if s3ops.path_exists(s3_client, bucket, dest):
            ui.warning(f"Path s3://{bucket}/{dest}/ already exists!")
            if not ui.confirm("Overwrite?", default=False):
                return folder_name

        ui.dim(f"  Uploading to s3://{bucket}/{dest}/")
        count = s3ops.upload_folder(s3_client, bucket, local_path, dest)
        ui.success(f"Uploaded {count} files to s3://{bucket}/{dest}/")
        return folder_name

    # No upload: ask for the folder name
    return ui.prompt("S3 folder name (the part after your object store prefix)")


def _model_folder_rows(s3_client, bucket, prefix, folders):
    rows = []
    for i, folder in enumerate(folders, 1):
        full = f"{prefix}/{folder}"
        count, size = s3ops.count_and_size(s3_client, bucket, full)
        rows.append([str(i), folder, str(count), s3ops.format_size(size)])
    return rows


# ============================================================================
# Step 2: Container Image
# ============================================================================

def step_container_image(current_image: str, dockerfile: str, auto: bool) -> str:
    ui.header("Step 2/5 — Container Image")

    if auto:
        if current_image:
            ui.success(f"Using image from config: {current_image}")
        else:
            ui.warning("No container image configured")
        return current_image

    ui.dim(f"  Current image: {current_image or 'not set'}")
    print()

    choices = ["Use current image", "Build & push new image", "Enter different image tag"]
    for i, c in enumerate(choices, 1):
        print(f"  [{i}] {c}")

    choice = ui.prompt("Select (1-3)", default="1")

    if choice == "1":
        ui.success(f"Using: {current_image}")
        return current_image

    if choice == "2":
        if not docker.is_running():
            ui.error("Docker is not running. Please start Docker Desktop and try again.")
            return current_image
        tag = ui.prompt("Image tag", default=current_image)
        if docker.build(tag, dockerfile):
            if ui.confirm("Push to registry?"):
                docker.push(tag)
        return tag

    if choice == "3":
        tag = ui.prompt("Full image tag (registry/name:version)")
        ui.success(f"Using: {tag}")
        return tag

    return current_image


# ============================================================================
# Step 3: Validate ArgoCD Sync + Scenario
# ============================================================================

def step_validate_scenario(client, scenario_id: str, auto: bool) -> str:
    """Check ArgoCD app health and verify the scenario exists. Returns validated scenario_id or ''."""
    ui.header("Step 3/5 — Validate ArgoCD & Scenario")

    # --- ArgoCD application status ---
    try:
        apps_result = client.applications.query()
        apps = list(apps_result.resources) if hasattr(apps_result, "resources") else []
    except Exception as e:
        ui.warning(f"Could not query ArgoCD applications: {e}")
        apps = []

    if apps:
        rows = []
        unhealthy = []
        for app in apps:
            try:
                status = client.applications.get_status(application_name=app.application_name)
                health = getattr(status, "health_status", "Unknown")
                sync = getattr(status, "sync_status", "Unknown")
                synced_at = getattr(status, "sync_finished_at", "N/A") or "N/A"
            except Exception:
                health, sync, synced_at = "Error", "Error", "N/A"
            rows.append([app.application_name, health, sync, str(synced_at)])
            if health != "Healthy" or sync != "Synced":
                unhealthy.append(app.application_name)

        ui.table(
            "ArgoCD Applications",
            [{"name": "Name", "style": "cyan"}, {"name": "Health", "style": "green"}, {"name": "Sync"}, {"name": "Last Synced", "style": "dim"}],
            rows,
        )

        if unhealthy:
            ui.warning(f"Unhealthy/out-of-sync apps: {', '.join(unhealthy)}")
            if not auto:
                if ui.confirm("Trigger a refresh on unhealthy apps?", default=True):
                    for app_name in unhealthy:
                        try:
                            client.applications.refresh(application_name=app_name)
                            ui.success(f"Refresh triggered for '{app_name}'")
                        except Exception as e:
                            ui.warning(f"Could not refresh '{app_name}': {e}")
                    ui.dim("  Note: sync may take a minute. Re-run deploy once synced.")
    else:
        ui.warning("No ArgoCD applications found. Run 'aic setup' first.")

    # --- Scenario validation ---
    try:
        scenarios_result = client.scenario.query()
        scenarios = list(scenarios_result.resources) if hasattr(scenarios_result, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query scenarios: {e}")
        return ""

    if not scenarios:
        ui.error("No scenarios registered in AI Core.")
        ui.dim("  This usually means the ArgoCD app has not synced the ServingTemplate yet.")
        ui.dim("  Check that your app/*.yaml ServingTemplates are committed and the ArgoCD app is synced.")
        return ""

    scenario_ids = [s.id for s in scenarios]
    rows = []
    for s in scenarios:
        rows.append([s.id, getattr(s, "name", ""), getattr(s, "description", "") or ""])
    ui.table(
        "Available Scenarios",
        [{"name": "ID", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "Description"}],
        rows,
    )

    if auto:
        if scenario_id in scenario_ids:
            ui.success(f"Scenario '{scenario_id}' found")
            return scenario_id
        ui.error(f"Scenario '{scenario_id}' not found in AI Core.")
        ui.dim(f"  Available: {', '.join(scenario_ids)}")
        ui.dim("  Check that the scenario_id label in your app/*.yaml matches your config.")
        return ""

    # Interactive: always let the user pick (default to configured scenario)
    print()
    for i, s in enumerate(scenarios, 1):
        marker = " ◀ (configured)" if s.id == scenario_id else ""
        print(f"  [{i}] {s.id}{marker}")
    default_idx = next((str(i) for i, s in enumerate(scenarios, 1) if s.id == scenario_id), "1")
    choice = ui.prompt(f"Select scenario (1-{len(scenarios)})", default=default_idx)
    try:
        idx = int(choice)
        if 1 <= idx <= len(scenarios):
            selected = scenarios[idx - 1]
            ui.success(f"Using scenario: {selected.id}")
            return selected.id
    except ValueError:
        pass
    return scenario_id

def _create_artifact(client, object_store: str, model_folder: str, scenario_id: str, auto: bool = False) -> str:
    # Fetch registered object store names so we use the correct one
    registered_stores = []
    try:
        secrets = client.object_store_secrets.query()
        registered_stores = [s.name for s in (secrets.resources if hasattr(secrets, "resources") else [])]
    except Exception:
        pass

    # Resolve the actual object store name
    if registered_stores:
        if object_store not in registered_stores:
            ui.warning(f"Object store '{object_store}' from config not found in AI Core.")
            ui.dim(f"  Registered object stores: {', '.join(registered_stores)}")
            if auto:
                # In auto mode, try the first registered store
                object_store = registered_stores[0]
                ui.dim(f"  Using: {object_store}")
            else:
                print()
                for i, name in enumerate(registered_stores, 1):
                    print(f"  [{i}] {name}")
                choice = ui.prompt(f"Select object store (1-{len(registered_stores)})", default="1")
                try:
                    idx = int(choice)
                    if 1 <= idx <= len(registered_stores):
                        object_store = registered_stores[idx - 1]
                except ValueError:
                    pass
        else:
            ui.dim(f"  Object store: {object_store}")

    if not auto:
        name = ui.prompt("Artifact name", default=model_folder or "model")
        folder = ui.prompt("S3 folder (after object store prefix)", default=model_folder)
    else:
        name = model_folder
        folder = model_folder

    url = f"ai://{object_store}/{folder}"
    description = f"Model artifact for {name}"

    if not auto:
        ui.dim(f"\n  URL: {url}")
        ui.dim(f"  This resolves to: <bucket>/<path-prefix-of-{object_store}>/{folder}/")
        if not ui.confirm(f"Create artifact '{name}'?"):
            return ""

    try:
        result = client.artifact.create(
            name=name,
            kind=Artifact.Kind.MODEL,
            url=url,
            description=description,
            scenario_id=scenario_id,
            labels=[],
        )
        ui.success(f"Artifact created: {name} (ID: {result.id})")
        return result.id
    except Exception as e:
        ui.error(f"Failed to create artifact: {e}")
        return ""


# ============================================================================
# Step 5: Configuration
# ============================================================================

def _select_executable(client, scenario_id: str, current_executable_id: str) -> str:
    """Query executables for the scenario and let the user pick. Returns executable_id."""
    try:
        result = client.executable.query(scenario_id=scenario_id)
        executables = list(result.resources) if hasattr(result, "resources") else []
    except Exception as e:
        ui.warning(f"Could not query executables: {e}")
        return current_executable_id

    if not executables:
        ui.warning(f"No executables found for scenario '{scenario_id}' — using config value")
        return current_executable_id

    if len(executables) == 1:
        exe = executables[0]
        ui.success(f"Executable: {exe.id}")
        return exe.id

    print()
    for i, exe in enumerate(executables, 1):
        marker = " ◀ (configured)" if exe.id == current_executable_id else ""
        print(f"  [{i}] {exe.id}{marker}")
    default_idx = next((str(i) for i, e in enumerate(executables, 1) if e.id == current_executable_id), "1")
    choice = ui.prompt(f"Select executable (1-{len(executables)})", default=default_idx)
    try:
        idx = int(choice)
        if 1 <= idx <= len(executables):
            selected = executables[idx - 1]
            ui.success(f"Using executable: {selected.id}")
            return selected.id
    except ValueError:
        pass
    return current_executable_id

def step_configuration(client, general: dict, params: dict, artifact_id: str, auto: bool, model_folder: str = "") -> str:
    """Create an AI Core configuration. Returns config ID."""
    ui.header("Step 4/5 — Configuration")

    scenario_id = general.get("scenario_id", "")
    executable_id = general.get("executable_id", "")

    if not auto:
        # Let user pick the executable for the selected scenario
        executable_id = _select_executable(client, scenario_id, executable_id)
        general["executable_id"] = executable_id

    # Use model folder from step 1 as default for modelVersion only if the template defines it
    if model_folder and "modelVersion" in params:
        params["modelVersion"] = model_folder

    if not auto:
        # Let user review/edit all params loaded from the template or config.
        # containerImage was already resolved in Step 2 — skip it here.
        ui.dim("  Review deployment parameters (press Enter to keep default):")
        print()
        for key in list(params.keys()):
            if key == "containerImage":
                ui.dim(f"  {key}: {params[key]}  (set in Step 2)")
                continue
            new_val = ui.prompt(f"  {key}", default=params[key])
            params[key] = new_val

    # Show summary
    print()
    ui.dim("  Configuration:")
    for k, v in params.items():
        ui.dim(f"    {k}: {v}")

    if not auto:
        if not ui.confirm("\nCreate this configuration?"):
            return ""

    model_version = params.get("modelVersion", "model")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    full_name = f"{scenario_id}-{model_version}-{timestamp}"

    parameter_bindings = [ParameterBinding(key=k, value=v) for k, v in params.items()]
    input_bindings = [InputArtifactBinding(key="modeluri", artifact_id=artifact_id)] if artifact_id else []

    try:
        response = client.configuration.create(
            name=full_name,
            scenario_id=scenario_id,
            executable_id=executable_id,
            parameter_bindings=parameter_bindings,
            input_artifact_bindings=input_bindings,
        )
        ui.success(f"Configuration created: {response.id}")
        return response.id
    except Exception as e:
        ui.error(f"Failed to create configuration: {e}")
        return ""


# ============================================================================
# Step 6: Deploy
# ============================================================================

def step_deploy(client, config_id: str, config: dict, auto: bool,
                save_config_fn=None) -> str:
    """Create or update deployment. Returns deployment_id or empty string."""
    ui.header("Step 5/5 — Deploy")

    if not config_id:
        ui.error("No configuration ID — cannot deploy")
        return ""

    deployment_id = config.get("deployment_id")

    if deployment_id:
        ui.dim(f"  Updating existing deployment: {deployment_id}")
        try:
            client.deployment.modify(deployment_id=deployment_id, configuration_id=config_id)
            ui.success(f"Deployment updated: {deployment_id}")
        except Exception as e:
            ui.error(f"Failed to update deployment: {e}")
            return ""
    else:
        if not auto and not ui.confirm("Create new deployment?"):
            return ""
        try:
            response = client.deployment.create(configuration_id=config_id)
            deployment_id = response.id
            ui.success(f"Deployment created: {deployment_id}")
        except Exception as e:
            ui.error(f"Failed to create deployment: {e}")
            return ""

    # Save config immediately after deployment is triggered — before monitoring
    if save_config_fn:
        save_config_fn(deployment_id)

    # Monitor
    if auto or ui.confirm("Monitor deployment until running?"):
        monitor(client, deployment_id)

    return deployment_id


def _offer_save_config(general, params, artifact_id, model_cfg, docker_cfg, deployment_id):
    """After an interactive deploy, offer to write/overwrite a deployment_config.yaml."""
    print()
    default_path = "deployment_config.yaml"
    existing = os.path.exists(default_path)
    prompt_text = f"{'Overwrite' if existing else 'Save'} deployment config to"
    if not ui.confirm(f"Save this deployment as a config file for easy re-deployment?", default=True):
        return

    path = ui.prompt(prompt_text, default=default_path)
    _save_config_yaml(path, general, params, artifact_id, model_cfg, docker_cfg, deployment_id)
    ui.success(f"Config saved → {path}")
    ui.dim(f"  Re-deploy with: aic deploy --auto --config {path}")


def _save_config_yaml(path, general, params, artifact_id, model_cfg, docker_cfg, deployment_id):
    lines = []
    lines.append("general:")
    lines.append(f'  name: "{general.get("name", general.get("scenario_id", ""))}"')
    lines.append(f'  scenario_id: "{general.get("scenario_id", "")}"')
    lines.append(f'  resource_group: "{general.get("resource_group", "default")}"')
    lines.append(f'  executable_id: "{general.get("executable_id", "")}"')
    lines.append("")
    lines.append("model:")
    lines.append(f'  object_store: "{model_cfg.get("object_store", "")}"')
    lines.append(f'  s3_key_file: "{model_cfg.get("s3_key_file", "")}"')
    lines.append(f'  s3_prefix: "{model_cfg.get("s3_prefix", "")}"')
    lines.append(f'  local_path: "{model_cfg.get("local_path", "")}"')
    lines.append("")
    lines.append("docker:")
    lines.append(f'  secret: "{docker_cfg.get("secret", "")}"')
    lines.append(f'  image: "{docker_cfg.get("image", "")}"')
    lines.append(f'  dockerfile: "{docker_cfg.get("dockerfile", "")}"')
    lines.append("")
    lines.append("parameter_bindings:")
    for key, value in params.items():
        if key == "containerImage":
            continue  # stored in docker.image — no need to duplicate
        lines.append(f'  - key: "{key}"')
        lines.append(f'    value: "{value}"')
    lines.append("")
    if artifact_id:
        lines.append("input_artifact_bindings:")
        lines.append(f'  - key: "modeluri"')
        lines.append(f'    artifact_id: "{artifact_id}"')
        lines.append("")
    if deployment_id:
        lines.append(f"# Uncomment to update this deployment instead of creating a new one:")
        lines.append(f"# deployment_id: {deployment_id}")
        lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))


def monitor(client, deployment_id: str):
    """Poll deployment status until RUNNING or failed.
    Prints detailed status to CLI each cycle and streams logs to a file."""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = logs_dir / f"{deployment_id}_{timestamp}.log"

    # Create the file immediately so it can be opened/tailed while deployment is pending
    log_file.touch()

    ui.dim(f"  Monitoring... (Ctrl+C to stop, deployment continues in background)")
    ui.dim(f"  Logs → {log_file}")
    print()

    start = time.time()
    seen_log_lines: set[str] = set()
    last_status = None
    _log_fh = open(log_file, "a", buffering=1)  # line-buffered — every write hits disk immediately

    def _fetch_logs() -> list[str]:
        try:
            response = client.deployment.query_logs(deployment_id=deployment_id)
            if response.data and response.data.result:
                return [item.msg for item in reversed(response.data.result)]
        except Exception:
            pass
        return []

    def _append_new_logs(lines: list[str]):
        new_lines = [l for l in lines if l not in seen_log_lines]
        if new_lines:
            seen_log_lines.update(new_lines)
            for line in new_lines:
                _log_fh.write(line + "\n")
        return new_lines

    try:
        while True:
            dep = client.deployment.get(deployment_id)
            status = dep.status.value
            elapsed = int(time.time() - start)
            mins, secs = elapsed // 60, elapsed % 60

            # Print status block on every change (or every cycle if details present)
            details = dep.status_details or {}
            reason = details.get("reason", "") if isinstance(details, dict) else ""
            message = details.get("message", "") if isinstance(details, dict) else ""

            if status != last_status or reason:
                print(f"  [{mins:02d}m{secs:02d}s]  Status: {status}", end="")
                if reason:
                    print(f"  |  {reason}", end="")
                if message:
                    print(f"\n           {message}", end="")
                print()
                last_status = status
            else:
                sys.stdout.write(f"\r  [{mins:02d}m{secs:02d}s]  Status: {status}   ")
                sys.stdout.flush()

            # Fetch and write new log lines to file
            all_logs = _fetch_logs()
            new_lines = _append_new_logs(all_logs)
            if new_lines:
                sys.stdout.write("\r")
                ui.dim(f"  → {len(new_lines)} new log line(s) written to {log_file}")

            if status == "RUNNING":
                print()
                ui.success(f"Deployment is RUNNING  ({mins}m {secs}s)")
                if dep.deployment_url:
                    ui.success(f"URL: {dep.deployment_url}")
                return

            if status in ("DEAD", "STOPPED"):
                print()
                ui.error(f"Deployment ended with status: {status}")
                if message:
                    ui.dim(f"  {message}")
                ui.dim(f"  Full logs: {log_file}")
                return

            time.sleep(30)

    except KeyboardInterrupt:
        print()
        ui.dim(f"  Stopped monitoring. Deployment continues in background.")
        ui.dim(f"  Logs so far: {log_file}")
        ui.dim(f"  Check status: aic deploy --status {deployment_id}")
        ui.dim(f"  Stream logs:  aic deploy --logs {deployment_id}")
    finally:
        _log_fh.close()


# ============================================================================
# Management commands
# ============================================================================

def list_deployments():
    """List all deployments."""
    client = create_client()
    ui.header("Deployments")

    try:
        result = client.deployment.query()
        deps = list(result.resources) if hasattr(result, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query deployments: {e}")
        return

    if not deps:
        ui.dim("  No deployments found.")
        return

    rows = []
    for dep in deps:
        status = dep.status.value
        url = (dep.deployment_url[:50] + "...") if dep.deployment_url else "N/A"
        rows.append([dep.id, status, dep.scenario_id or "N/A", url])

    ui.table(
        "All Deployments",
        [{"name": "ID", "style": "cyan"}, {"name": "Status", "style": "green"}, {"name": "Scenario"}, {"name": "URL"}],
        rows,
    )


def list_artifacts():
    """List all model artifacts."""
    client = create_client()
    ui.header("Model Artifacts")

    try:
        result = client.artifact.query(kind=Artifact.Kind.MODEL)
        artifacts = list(result.resources) if hasattr(result, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query artifacts: {e}")
        return

    if not artifacts:
        ui.dim("  No model artifacts found.")
        return

    rows = []
    for a in artifacts:
        created = str(getattr(a, "created_at", "N/A"))[:19]
        rows.append([a.id, a.name, a.scenario_id or "N/A", a.url, created])

    ui.table(
        "All Model Artifacts",
        [{"name": "ID", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "Scenario"}, {"name": "URL"}, {"name": "Created", "style": "dim"}],
        rows,
    )


def list_configurations():
    """List all configurations."""
    client = create_client()
    ui.header("Configurations")

    try:
        result = client.configuration.query()
        configs = list(result.resources) if hasattr(result, "resources") else []
    except Exception as e:
        ui.error(f"Failed to query configurations: {e}")
        return

    if not configs:
        ui.dim("  No configurations found.")
        return

    rows = []
    for c in configs:
        created = str(getattr(c, "created_at", "N/A"))[:19]
        rows.append([c.id, c.name, c.scenario_id or "N/A", c.executable_id or "N/A", created])

    ui.table(
        "All Configurations",
        [{"name": "ID", "style": "cyan"}, {"name": "Name", "style": "green"}, {"name": "Scenario"}, {"name": "Executable"}, {"name": "Created", "style": "dim"}],
        rows,
    )


def check_status(deployment_id: str):
    """Show detailed status for a deployment."""
    client = create_client()

    try:
        dep = client.deployment.get(deployment_id)
    except Exception as e:
        ui.error(f"Failed to get deployment: {e}")
        return

    ui.header(f"Deployment: {deployment_id}")
    ui.dim(f"  Status:        {dep.status.value}")
    ui.dim(f"  Scenario:      {dep.scenario_id}")
    ui.dim(f"  Configuration: {dep.configuration_id}")
    ui.dim(f"  URL:           {dep.deployment_url or 'N/A'}")
    ui.dim(f"  Created:       {getattr(dep, 'created_at', 'N/A')}")

    if dep.status_details:
        print()
        ui.dim(f"  Details:\n{json.dumps(dep.status_details, indent=2)}")


def stop_deployment(deployment_id: str):
    """Stop a running deployment."""
    client = create_client()

    if not ui.confirm(f"Stop deployment {deployment_id}?", default=False):
        return

    try:
        client.deployment.modify(deployment_id=deployment_id, target_status=TargetStatus.STOPPED)
        ui.success(f"Deployment {deployment_id} stop requested")
    except Exception as e:
        ui.error(f"Failed to stop deployment: {e}")


def query_logs(deployment_id: str, since_minutes: int = 60):
    """Fetch deployment logs, print to stdout, and write to logs/<deployment_id>_<timestamp>.log."""
    client = create_client()

    try:
        response = client.deployment.query_logs(deployment_id=deployment_id)

        if not response.data or response.data.result is None:
            ui.dim(f"  No logs found.")
            return

        lines = [item.msg for item in reversed(response.data.result)]

        if not lines:
            ui.dim(f"  No logs found.")
            return

        # Write to logs/
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = logs_dir / f"{deployment_id}_{timestamp}.log"
        log_file.write_text("\n".join(lines) + "\n")

        # Print to stdout
        for line in lines:
            print(line)

        ui.success(f"Logs saved → {log_file}  ({len(lines)} lines)")

    except Exception as e:
        ui.error(f"Failed to fetch logs: {e}")


def update_deployment(deployment_id: str, config_path: str = CONFIG_PATH):
    """Update an existing deployment with a new model artifact and configuration."""
    config = load_config(config_path)
    client = create_client()

    model_cfg = config.get("model", {})
    general = config.get("general", {})
    params = {p["key"]: p["value"] for p in config.get("parameter_bindings", [])}
    object_store = model_cfg.get("object_store", "")
    scenario_id = general.get("scenario_id", "")

    ui.banner("aic deploy --update", f"Update deployment: {deployment_id}")

    # Check for existing artifact in config, otherwise let user select/create
    artifact_bindings = config.get("input_artifact_bindings", [])
    artifact_id = artifact_bindings[0].get("artifact_id", "") if artifact_bindings else ""

    if artifact_id:
        ui.success(f"Using artifact from config: {artifact_id}")
    else:
        # Let user pick or create an artifact interactively
        s3_key_file = model_cfg.get("s3_key_file", str(s3ops.DEFAULT_S3_KEY()))
        s3_prefix = model_cfg.get("s3_prefix", s3ops.DEFAULT_PREFIX)
        artifact_id, _ = step_model_artifact_combined(
            client, object_store, s3_key_file, s3_prefix, "", scenario_id, config, auto=False
        )

    if not artifact_id:
        ui.error("No artifact selected — cannot update deployment.")
        return

    config_id = step_configuration(client, general, params, artifact_id, auto=False)
    if not config_id:
        return

    try:
        client.deployment.modify(deployment_id=deployment_id, configuration_id=config_id)
        ui.success(f"Deployment {deployment_id} updated with config {config_id}")
        if ui.confirm("Monitor deployment?"):
            monitor(client, deployment_id)
    except Exception as e:
        ui.error(f"Failed to update deployment: {e}")
