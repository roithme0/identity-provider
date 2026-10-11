from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast


def compose_arguments(compose_file: Path) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    expected = root / "deployments/docker-compose-test.yml"
    if compose_file.resolve() != expected.resolve():
        raise RuntimeError("Verification requires deployments/docker-compose-test.yml.")
    return [
        "docker", "compose", "--project-name", "identity-provider-test",
        "--env-file", str(root / "deployments/.env.example"), "-f", str(expected),
    ]


def configuration(compose_file: Path) -> dict[str, object]:
    result = subprocess.run(
        [*compose_arguments(compose_file), "config", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("Cannot resolve the disposable Compose configuration.")
    value: object = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get("name") != "identity-provider-test" or value.get("x-disposable-test") is not True:
        raise RuntimeError("Expected the isolated disposable test project.")
    return cast(dict[str, object], value)


def require_disposable(compose_file: Path, base_url: str) -> None:
    configuration(compose_file)
    if base_url.rstrip("/") != "http://localhost:8016":
        raise RuntimeError("Verification requires the disposable provider at http://localhost:8016.")
