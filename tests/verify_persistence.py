from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from verify_local import Browser, admin, authorized, json_object, users


def compose(compose_file: Path, *arguments: str) -> str:
    command = ["docker", "compose", "-f", str(compose_file)]
    env_file = compose_file.parent / ".env"
    if env_file.is_file():
        command.extend(["--env-file", str(env_file)])
    result = subprocess.run(
        [*command, *arguments], capture_output=True, text=True,
        encoding="utf-8", timeout=180, check=False,
    )
    if result.returncode:
        raise RuntimeError("Compose operation failed; inspect the local deployment status.")
    return result.stdout.strip()


def database_volume(container_id: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", container_id, "--format", "{{json .Mounts}}"],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    mounts = json.loads(result.stdout)
    assert isinstance(mounts, list)
    for item in mounts:
        mount = json_object(item)
        if mount.get("Destination") == "/var/lib/postgresql/data":
            assert mount.get("Type") == "volume", "Database storage must use a named volume."
            name = mount.get("Name")
            assert isinstance(name, str) and name
            return name
    raise AssertionError("Persistent database volume is missing.")


def wait_for_realm(base_url: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{base_url.rstrip('/')}/realms/services", timeout=5) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError("The persisted services realm did not become available within 120 seconds.")


def verify(base_url: str, compose_file: Path, allow_recreate: bool) -> None:
    if not allow_recreate:
        raise RuntimeError("Pass --allow-recreate to acknowledge local downtime and a remaining test account.")
    if urlparse(base_url).hostname not in ("localhost", "127.0.0.1"):
        raise RuntimeError("Persistence verification is restricted to localhost.")

    expected = json_object(json.loads(
        (Path(__file__).resolve().parents[1] / "config/services-realm.json").read_text(encoding="utf-8")
    ))
    realm = json_object(admin(compose_file, "get", "realms/services"))
    realm_id = realm["id"]
    for key, value in expected.items():
        assert realm.get(key) == value, f"Realm setting differs: {key}"
    profile = admin(compose_file, "get", "users/profile", "-r", "services")
    clients = admin(compose_file, "get", "clients", "-r", "services", "--fields", "id")
    run = secrets.token_hex(6)
    email = f"persistence-check-{run}@example.invalid"
    password = f"persistent cooking family {secrets.token_hex(16)}"
    assert authorized(Browser(base_url).register(email, password, "Persistence Check"))
    original = users(compose_file, email)
    assert len(original) == 1
    assert authorized(Browser(base_url).login(email, password))
    old_postgres = compose(compose_file, "ps", "-q", "postgres")
    old_keycloak = compose(compose_file, "ps", "-q", "keycloak")
    volume = database_volume(old_postgres)
    print("PASS: registered test account and confirmed named database volume", flush=True)

    compose(compose_file, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120", "postgres", "keycloak")
    print("Recreated PostgreSQL and Keycloak; waiting for the persisted realm", flush=True)
    wait_for_realm(base_url)
    new_postgres = compose(compose_file, "ps", "-q", "postgres")
    assert new_postgres != old_postgres
    assert compose(compose_file, "ps", "-q", "keycloak") != old_keycloak
    assert database_volume(new_postgres) == volume
    persisted = json_object(admin(compose_file, "get", "realms/services"))
    assert persisted["id"] == realm_id
    for key, value in expected.items():
        assert persisted.get(key) == value, f"Realm setting lost after recreation: {key}"
    assert admin(compose_file, "get", "users/profile", "-r", "services") == profile
    assert admin(compose_file, "get", "clients", "-r", "services", "--fields", "id") == clients
    preserved = users(compose_file, email)
    assert len(preserved) == 1 and preserved[0]["id"] == original[0]["id"]
    assert preserved[0].get("attributes") == original[0].get("attributes")
    assert authorized(Browser(base_url).login(email, password)), "Password login failed after recreation."
    print("PASS: realm, profile, clients, user ID and password login survived both container replacements")
    print(f"A test account remains: {email}. Its password is not printed or persisted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify database persistence by replacing the local containers.")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--compose-file", type=Path, default=Path(__file__).resolve().parents[1] / "deployments/docker-compose-local.yml")
    parser.add_argument("--allow-recreate", action="store_true")
    args = parser.parse_args()
    verify(args.base_url, args.compose_file.resolve(), args.allow_recreate)
