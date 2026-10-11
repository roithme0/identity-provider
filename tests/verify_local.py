from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPCookieProcessor


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.inputs: dict[str, str] = {}
        self.registration = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.action = values.get("action") or ""
        if tag == "input" and values.get("name"):
            self.inputs[values["name"] or ""] = values.get("value") or ""
        if tag == "a" and "registration" in (values.get("href") or ""):
            self.registration = values["href"] or ""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        return None


@dataclass(frozen=True)
class Response:
    body: str
    location: str


class Browser:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        if urlparse(base_url).hostname not in ("localhost", "127.0.0.1"):
            raise RuntimeError("The HTTP cookie exception is restricted to local verification.")
        cookie_policy = DefaultCookiePolicy(secure_protocols=("https", "wss", "http"))
        self.opener = build_opener(HTTPCookieProcessor(CookieJar(cookie_policy)), NoRedirect())

    def request(self, url: str, fields: dict[str, str] | None = None) -> Response:
        data = urlencode(fields).encode() if fields is not None else None
        for _ in range(6):
            try:
                with self.opener.open(Request(url, data=data), timeout=20) as response:
                    result = Response(response.read().decode(), response.headers.get("Location", ""))
            except HTTPError as error:
                if error.code not in (302, 303, 400):
                    raise RuntimeError(f"Unexpected HTTP status {error.code}") from None
                result = Response(error.read().decode(), error.headers.get("Location", ""))
            if not result.location or authorized(result):
                return result
            url = urljoin(url, result.location)
            if urlparse(url).netloc != urlparse(self.base_url).netloc:
                raise RuntimeError("Unexpected redirect outside the local Keycloak server")
            data = None
        raise RuntimeError("Too many redirects")

    def login_page(self) -> Page:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = urlencode({
            "client_id": "account-console",
            "redirect_uri": f"{self.base_url}/realms/services/account/",
            "response_type": "code", "scope": "openid", "state": secrets.token_urlsafe(20),
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        response = self.request(f"{self.base_url}/realms/services/protocol/openid-connect/auth?{query}")
        page = Page()
        page.feed(response.body)
        assert page.action, "Hosted login form was not returned. Check the base URL."
        return page

    def registration_page(self) -> Page:
        login = self.login_page()
        assert login.registration, "Self-registration link is missing."
        response = self.request(urljoin(self.base_url, login.registration))
        page = Page()
        page.feed(response.body)
        assert page.action, "Hosted registration form was not returned."
        assert "username" not in page.inputs, "Username must not be collected."
        assert "firstName" not in page.inputs and "lastName" not in page.inputs
        return page

    def register(self, email: str, password: str, display_name: str) -> Response:
        page = self.registration_page()
        display_field = next((name for name in page.inputs if name.endswith("displayName")), "")
        assert display_field, "Display-name field is missing."
        fields = page.inputs | {
            "email": email, "password": password, "password-confirm": password,
            display_field: display_name,
        }
        return self.request(page.action, fields)

    def login(self, email: str, password: str) -> Response:
        page = self.login_page()
        return self.request(page.action, page.inputs | {"username": email, "password": password})


def authorized(response: Response) -> bool:
    return "code" in parse_qs(urlparse(response.location).query)


def admin(compose_file: Path, *arguments: str) -> object:
    shell = (
        'export KC_CLI_PASSWORD="${IDP_ADMIN_PASSWORD:-$KC_BOOTSTRAP_ADMIN_PASSWORD}"; '
        'exec /opt/keycloak/bin/kcadm.sh "$@" --no-config '
        '--server "${IDP_ADMIN_SERVER:-http://localhost:8080}" '
        '--realm "${IDP_ADMIN_REALM:-master}" '
        '--user "${IDP_ADMIN_USERNAME:-$KC_BOOTSTRAP_ADMIN_USERNAME}"'
    )
    compose_arguments = ["docker", "compose", "-f", str(compose_file)]
    env_file = compose_file.parent / ".env"
    if env_file.is_file():
        compose_arguments.extend(["--env-file", str(env_file)])
    result = subprocess.run(
        [*compose_arguments, "exec", "-T", "keycloak",
         "bash", "-c", shell, "kcadm", *arguments],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError("Admin verification failed; check administrator credentials and server availability.")
    return json.loads(result.stdout) if result.stdout.strip() else None


def json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError("Expected a JSON object")
    return cast(dict[str, object], value)


def users(compose_file: Path, email: str) -> list[dict[str, object]]:
    value = admin(compose_file, "get", "users", "-r", "services", "-q", f"email={email}", "-q", "exact=true")
    if not isinstance(value, list):
        raise TypeError("Expected a JSON array")
    return [json_object(item) for item in value]


def verify(base_url: str, compose_file: Path, allow_user_creation: bool) -> None:
    if not allow_user_creation:
        raise RuntimeError("Pass --allow-user-creation to acknowledge that demo test accounts will remain.")
    if urlparse(base_url).hostname not in ("localhost", "127.0.0.1"):
        raise RuntimeError("This verification is restricted to a local demo.")

    root = Path(__file__).resolve().parents[1]
    expected = json_object(json.loads((root / "config/services-realm.json").read_text(encoding="utf-8")))
    current = json_object(admin(compose_file, "get", "realms/services"))
    for key, value in expected.items():
        assert current.get(key) == value, f"Realm setting differs: {key}"
    profile = json_object(admin(compose_file, "get", "users/profile", "-r", "services"))
    profile_attributes = profile.get("attributes")
    assert isinstance(profile_attributes, list)
    assert {json_object(item).get("name") for item in profile_attributes} == {"username", "email", "displayName"}
    client_snapshot = admin(compose_file, "get", "clients", "-r", "services", "--fields", "id")
    actions = admin(compose_file, "get", "authentication/required-actions", "-r", "services")
    assert isinstance(actions, list)
    for item in actions:
        action = json_object(item)
        if action.get("alias") in ("VERIFY_EMAIL", "UPDATE_EMAIL"):
            assert action.get("enabled") is False and action.get("defaultAction") is False
    print("PASS: managed realm settings and disabled email actions")

    run = secrets.token_hex(6)
    email = f"auth-check-{run}-a@example.invalid"
    second_email = f"auth-check-{run}-b@example.invalid"
    password = f"quiet cooking family {run}"
    for name, attempted_password, display_name in (
        ("short password", "a" * 14, "Kitchen Friend"),
        ("blank display name", password, "   "),
        ("long display name", password, "a" * 51),
        ("control character", password, "Kitchen\nFriend"),
    ):
        response = Browser(base_url).register(email, attempted_password, display_name)
        assert not authorized(response), f"Registration accepted {name}"
        assert not users(compose_file, email), f"Invalid registration persisted a user: {name}"
    print("PASS: short password and invalid display names rejected")

    display_name = "  Küchen Freund  "
    assert authorized(Browser(base_url).register(email, password, display_name)), "Valid registration failed"
    existing = users(compose_file, email)
    assert len(existing) == 1 and existing[0].get("emailVerified") is False
    assert existing[0].get("username") == email
    original_id = existing[0]["id"]
    attributes = json_object(existing[0].get("attributes"))
    stored_name = attributes.get("displayName")
    assert stored_name == [display_name], "Unexpected display-name storage behavior; review normalization."
    print("PASS: immediate registration; stored display name retains surrounding whitespace")
    assert authorized(Browser(base_url).register(second_email, password, display_name)), "Duplicate display names rejected"
    assert not authorized(Browser(base_url).register(email, password, "Other Name")), "Duplicate email accepted"
    assert len(users(compose_file, email)) == 1
    assert authorized(Browser(base_url).login(email, password)), "Email/password login failed"
    print("PASS: duplicate display names allowed; duplicate email rejected; email/password login works")

    admin(compose_file, "update", "realms/services", "-s", "passwordPolicy=length(16)")
    assert json_object(admin(compose_file, "get", "realms/services")).get("passwordPolicy") == "length(16)"
    applied = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(root / "scripts/Apply-Configuration.ps1"),
         "-ComposeFile", str(compose_file)], capture_output=True, text=True, encoding="utf-8", timeout=90, check=False,
    )
    assert applied.returncode == 0, "Configuration reapply failed"
    preserved = users(compose_file, email)
    assert len(preserved) == 1 and preserved[0]["id"] == original_id
    assert json_object(admin(compose_file, "get", "realms/services")).get("passwordPolicy") == "length(15)"
    assert admin(compose_file, "get", "clients", "-r", "services", "--fields", "id") == client_snapshot
    assert authorized(Browser(base_url).login(email, password)), "Login failed after reapply"
    print("PASS: repeat apply corrects managed-setting drift and preserves users, clients and login")
    print(f"Verification completed. Two demo accounts remain with prefix auth-check-{run}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify the running local realm using its hosted login forms.")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--compose-file", type=Path, default=Path(__file__).resolve().parents[1] / "deployments" / "docker-compose-local.yml")
    parser.add_argument("--allow-user-creation", action="store_true")
    args = parser.parse_args()
    verify(args.base_url, args.compose_file.resolve(), args.allow_user_creation)
