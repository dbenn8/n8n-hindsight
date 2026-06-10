#!/usr/bin/env python3
"""Create or update the dedicated Appliku validator app for n8n-hindsight."""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import quote

from appliku import Appliku
from appliku._exceptions import parse_error_response
from appliku._generated.models.application_request import ApplicationRequest
from appliku._generated.models.patched_application_request import (
    PatchedApplicationRequest,
)


DEFAULT_TEAM = "daniel-bennett-svtnoxta"
DEFAULT_SOURCE_APP_ID = 4460
DEFAULT_VALIDATOR_APP_NAME = "n8nvalidator"
DEFAULT_DOCKERFILE_PATH = "validator-app/Dockerfile"
DEFAULT_YML_PATH = "validator-app/appliku.yml"
DEFAULT_CONTAINER_PORT = 8000


def _build_payload(source_app: Any, branch: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "branch": branch,
        "repository_provider": source_app.repository_provider,
        "repository_name": source_app.repository_name,
        "build_pack": source_app.build_pack,
        "deployment_mode": source_app.deployment_mode,
        "push_to_deploy": source_app.push_to_deploy,
        "expose_web_port": True,
        "container_port": DEFAULT_CONTAINER_PORT,
        "dockerfile_path": DEFAULT_DOCKERFILE_PATH,
        "yml_config_file_path": DEFAULT_YML_PATH,
        "is_updating_nginx_enabled": True,
        "is_disabled_default_subdomain": False,
    }
    if source_app.server is not None:
        payload["server"] = source_app.server
    if source_app.cluster is not None:
        payload["cluster"] = source_app.cluster
    return payload


def _request_json_app(
    client: Appliku,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    http = client._client.get_httpx_client()
    kwargs: dict[str, Any] = {}
    if payload is not None:
        kwargs["json"] = payload
    response = http.request(method, url, **kwargs)
    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = {"detail": response.text}
        raise parse_error_response(response.status_code, body)
    return response.json()


def _activate_yaml_config(client: Appliku, team: str, app_id: int, yml_path: str) -> dict[str, Any]:
    with open(yml_path, "r", encoding="utf-8") as fh:
        config_lines = fh.read().splitlines()
    return _request_json_app(
        client,
        "PATCH",
        f"/api/team/{quote(team, safe='')}/applications/{app_id}/validate_yaml_config",
        {"config_data": config_lines},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", default=DEFAULT_TEAM)
    parser.add_argument("--source-app-id", type=int, default=DEFAULT_SOURCE_APP_ID)
    parser.add_argument("--app-name", default=DEFAULT_VALIDATOR_APP_NAME)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--main-app-id", type=int, default=DEFAULT_SOURCE_APP_ID)
    parser.add_argument(
        "--configure-main-forward-url",
        action="store_true",
        help="Set WORKFLOW_VALIDATOR_FORWARD_URL on the main app after create/update.",
    )
    parser.add_argument("--deploy-validator", action="store_true")
    parser.add_argument("--deploy-main", action="store_true")
    args = parser.parse_args()

    client = Appliku()
    source_app = client.apps.get(args.team, app_id=args.source_app_id)
    branch = args.branch or source_app.branch
    payload = _build_payload(source_app, branch)

    existing = next(
        (app for app in client.apps.list(args.team) if app.name == args.app_name),
        None,
    )
    if existing is None:
        body = ApplicationRequest(name=args.app_name, **payload).to_dict()
        created = _request_json_app(
            client,
            "POST",
            f"/api/team/{quote(args.team, safe='')}/applications/create/",
            body,
        )
        validator_app = client.apps.get(args.team, app_id=created["id"])
        action = "created"
    else:
        body = PatchedApplicationRequest(**payload).to_dict()
        updated = _request_json_app(
            client,
            "PATCH",
            f"/api/team/{quote(args.team, safe='')}/applications/{existing.id}/",
            body,
        )
        validator_app = client.apps.get(args.team, app_id=updated["id"])
        action = "updated"

    yaml_state = _activate_yaml_config(client, args.team, validator_app.id, DEFAULT_YML_PATH)

    print(
        f"{action} validator app {validator_app.name} "
        f"(id={validator_app.id}, subdomain={validator_app.default_subdomain})"
    )
    print(
        "yaml config state:",
        yaml_state.get("config_file_status_display"),
        f"(status={yaml_state.get('config_file_status')})",
    )

    if args.configure_main_forward_url:
        forward_url = f"https://{validator_app.default_subdomain}/public/validate-workflow"
        client.apps.set_config_vars(
            args.team,
            args.main_app_id,
            {"WORKFLOW_VALIDATOR_FORWARD_URL": forward_url},
        )
        print(
            f"configured main app {args.main_app_id} "
            f"WORKFLOW_VALIDATOR_FORWARD_URL={forward_url}"
        )

    if args.deploy_validator:
        result = _request_json_app(
            client,
            "POST",
            f"/api/team/{quote(args.team, safe='')}/applications/{validator_app.id}/deploy",
        )
        print(f"triggered validator deployment: {result}")

    if args.deploy_main:
        result = _request_json_app(
            client,
            "POST",
            f"/api/team/{quote(args.team, safe='')}/applications/{args.main_app_id}/deploy",
        )
        print(f"triggered main app deployment: {result}")


if __name__ == "__main__":
    main()
