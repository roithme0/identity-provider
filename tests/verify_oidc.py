from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import jwt

from verify_local import Browser, Page, Response, admin, authorized, json_object, users


@dataclass
class Callback:
    path: str
    state: str = ""
    code: str = ""


@contextmanager
def callback_listener(url: str) -> Iterator[Callback]:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.port:
        raise RuntimeError("Verification requires an HTTP callback on an explicit loopback port.")
    callback = Callback(parsed.path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested = urlparse(self.path)
            fields = parse_qs(requested.query)
            valid = (
                requested.path == callback.path
                and bool(callback.state)
                and fields.get("state") == [callback.state]
                and len(fields.get("code", [])) == 1
                and bool(fields["code"][0])
                and "error" not in fields
            )
            if valid:
                callback.code = fields["code"][0]
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Local OIDC verification callback.")

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", parsed.port), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield callback
        finally:
            server.shutdown()
            thread.join(timeout=5)


class KochwikiBrowser(Browser):
    def __init__(self, base_url: str, callback_url: str) -> None:
        super().__init__(base_url)
        self.callback_url = callback_url
        self.verifier = secrets.token_urlsafe(48)
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)

    def authorization_url(self, *, pkce: bool = True, callback_url: str | None = None) -> str:
        query = {
            "client_id": "kochwiki",
            "redirect_uri": callback_url or self.callback_url,
            "response_type": "code",
            "scope": "openid",
            "state": self.state,
            "nonce": self.nonce,
        }
        if pkce:
            query["code_challenge"] = base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()
            ).decode().rstrip("=")
            query["code_challenge_method"] = "S256"
        return f"{self.base_url}/realms/services/protocol/openid-connect/auth?{urlencode(query)}"

    def login_page(self) -> Page:
        response = self.request(self.authorization_url())
        page = Page()
        page.feed(response.body)
        assert page.action, "Kochwiki hosted login form was not returned."
        return page

    def receive_code(self, response: Response, callback: Callback) -> str:
        destination = urlparse(response.location)
        expected = urlparse(self.callback_url)
        assert (destination.scheme, destination.netloc, destination.path) == (
            expected.scheme, expected.netloc, expected.path
        ), "Unexpected login callback destination."
        assert authorized(response), "Login did not produce an authorization code."
        callback.state = self.state
        callback.code = ""
        with urlopen(response.location, timeout=10) as received:
            assert received.status == 200
        assert callback.code, "The callback did not receive a code."
        return callback.code


def compose_configuration(compose_file: Path) -> dict[str, object]:
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(compose_file.parent / ".env"),
         "-f", str(compose_file), "config", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("Cannot resolve the local Compose configuration.")
    return json_object(json.loads(result.stdout))


def client(compose_file: Path) -> dict[str, object]:
    found = admin(compose_file, "get", "clients", "-r", "services", "-q", "clientId=kochwiki")
    assert isinstance(found, list) and len(found) == 1, "Expected one Kochwiki client."
    return json_object(admin(compose_file, "get", f"clients/{json_object(found[0])['id']}", "-r", "services"))


def token_request(endpoint: str, fields: dict[str, str]) -> tuple[int, dict[str, object]]:
    request = Request(endpoint, data=urlencode(fields).encode())
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json_object(json.loads(response.read()))
    except HTTPError as error:
        return error.code, json_object(json.loads(error.read()))


def exchange_fields(secret: str, callback_url: str, code: str, verifier: str) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "client_id": "kochwiki",
        "client_secret": secret,
        "redirect_uri": callback_url,
        "code": code,
        "code_verifier": verifier,
    }


def validate_id_token(
    tokens: dict[str, object], issuer: str, jwks_url: str, nonce: str,
    expected_email: str, expected_name: str,
) -> dict[str, object]:
    encoded = tokens.get("id_token")
    assert isinstance(encoded, str), "No ID token returned."
    key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(encoded).key
    claims = json_object(jwt.decode(
        encoded, key, algorithms=["RS256"], issuer=issuer, audience="kochwiki",
        options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
    ))
    assert claims["nonce"] == nonce, "ID-token nonce does not match the login attempt."
    assert isinstance(claims["sub"], str) and claims["sub"], "Missing subject."
    assert claims.get("email") == expected_email, "Email claim differs."
    assert claims.get("email_verified") is False, "Unverified registration was not retained."
    assert claims.get("displayName") == expected_name, "Display-name claim differs."
    assert "realm_access" not in claims and "resource_access" not in claims
    return claims


def reapply(root: Path, compose_file: Path) -> None:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(root / "scripts/Apply-Configuration.ps1"),
         "-ComposeFile", str(compose_file)],
        capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
    )
    assert result.returncode == 0, "Repeat configuration apply failed; rerun the apply script."


def verify(base_url: str | None, compose_file: Path, callback_url: str | None, allow_creation: bool) -> None:
    if not allow_creation:
        raise RuntimeError("Pass --allow-user-creation; one local verification account will remain.")
    if compose_file.name != "docker-compose-local.yml":
        raise RuntimeError("This verification is restricted to the local Compose deployment.")
    root = Path(__file__).resolve().parents[1]
    configuration = compose_configuration(compose_file)
    settings = json_object(configuration["x-kochwiki"])
    secret = settings["client-secret"]
    assert isinstance(secret, str) and len(secret) >= 32 and secret != "xxxx"
    services = json_object(configuration["services"])
    keycloak = json_object(services["keycloak"])
    environment = json_object(keycloak["environment"])
    public_url = environment["KC_HOSTNAME"]
    assert isinstance(public_url, str)
    base_url = (base_url or public_url).rstrip("/")
    assert urlparse(base_url).hostname in ("localhost", "127.0.0.1")
    configured_callback = settings["test-callback-url"]
    assert isinstance(configured_callback, str)
    callback_url = callback_url or configured_callback
    issuer = f"{base_url}/realms/services"
    with urlopen(f"{issuer}/.well-known/openid-configuration", timeout=20) as response:
        discovery = json_object(json.loads(response.read()))
    assert discovery["issuer"] == issuer, "Use the canonical KC_HOSTNAME URL."
    token_endpoint = f"{issuer}/protocol/openid-connect/token"
    jwks_url = f"{issuer}/protocol/openid-connect/certs"
    assert discovery["token_endpoint"] == token_endpoint and discovery["jwks_uri"] == jwks_url

    current = client(compose_file)
    client_id = current["id"]
    assert isinstance(client_id, str)
    expected = json_object(json.loads((root / "config/kochwiki/kochwiki-client.json").read_text()))
    for field, value in expected.items():
        if field == "attributes":
            attributes = json_object(current[field])
            for name, setting in json_object(value).items():
                assert attributes.get(name) == setting, f"Client attribute differs: {name}"
        else:
            actual = current.get(field, False) if field == "authorizationServicesEnabled" else current.get(field)
            assert actual == value, f"Client setting differs: {field}"
    redirects = current["redirectUris"]
    assert isinstance(redirects, list) and callback_url in redirects
    allowed = settings["redirect-uris"]
    assert isinstance(allowed, str)
    assert set(redirects) == set(allowed.split() + [configured_callback])
    logout_urls = settings["logout-redirect-uris"]
    assert isinstance(logout_urls, str)
    assert json_object(current["attributes"])["post.logout.redirect.uris"] == "##".join(logout_urls.split())
    mapper_snapshot = admin(compose_file, "get", f"clients/{client_id}/protocol-mappers/models", "-r", "services")
    assert isinstance(mapper_snapshot, list) and len(mapper_snapshot) == 3
    print("PASS: confidential client, exact URL allowlists, PKCE and identity-only mappers")

    for label, url in (
        ("missing PKCE", KochwikiBrowser(base_url, callback_url).authorization_url(pkce=False)),
        ("unregistered callback", KochwikiBrowser(base_url, callback_url).authorization_url(
            callback_url="http://localhost:8765/unregistered"
        )),
    ):
        try:
            with Browser(base_url).opener.open(Request(url), timeout=20) as received:
                response = Response(received.read().decode(), received.headers.get("Location", ""))
        except HTTPError as error:
            assert error.code in (302, 303, 400)
            response = Response(error.read().decode(), error.headers.get("Location", ""))
        page = Page()
        page.feed(response.body)
        assert not authorized(response) and not page.action, f"Accepted {label}."
    print("PASS: missing PKCE and unregistered callbacks rejected")

    run = secrets.token_hex(6)
    email = f"oidc-check-{run}@example.invalid"
    password = f"quiet cooking family {run}"
    display_name = "  Kitchen Friend  "
    with callback_listener(callback_url) as callback:
        browser = KochwikiBrowser(base_url, callback_url)
        response = browser.register(email, password, display_name)
        callback.state = browser.state
        wrong_state_url = callback_url + "?" + urlencode({"code": "dummy", "state": "wrong"})
        try:
            urlopen(wrong_state_url, timeout=10).close()
            raise AssertionError("Callback accepted an incorrect state.")
        except HTTPError as error:
            assert error.code == 400 and not callback.code
        code = browser.receive_code(response, callback)
        fields = exchange_fields(secret, callback_url, code, browser.verifier)
        status, tokens = token_request(token_endpoint, fields)
        assert status == 200, "Authorization-code exchange failed."
        claims = validate_id_token(tokens, issuer, jwks_url, browser.nonce, email, display_name)
        subject = claims["sub"]
        assert users(compose_file, email)[0]["id"] == subject
        status, error = token_request(token_endpoint, fields)
        assert status == 400 and error.get("error") == "invalid_grant", "Authorization code was reusable."
        print("PASS: hosted registration, callback state, single-use code, signed ID token and profile claims")

        for label, supplied_secret, supplied_verifier in (
            ("wrong client secret", "invalid-client-secret", None),
            ("wrong PKCE verifier", secret, secrets.token_urlsafe(48)),
        ):
            browser = KochwikiBrowser(base_url, callback_url)
            code = browser.receive_code(browser.login(email, password), callback)
            status, error = token_request(
                token_endpoint,
                exchange_fields(supplied_secret, callback_url, code, supplied_verifier or browser.verifier),
            )
            assert status in (400, 401) and error.get("error") in ("invalid_client", "unauthorized_client", "invalid_grant"), (
                f"Token exchange accepted {label}."
            )
        print("PASS: incorrect client secret and PKCE verifier rejected")

        display_mapper = next(
            json_object(item) for item in mapper_snapshot
            if json_object(item).get("name") == "kochwiki-mapper-display-name"
        )
        mapper_id = display_mapper["id"]
        assert isinstance(mapper_id, str)
        try:
            admin(compose_file, "update", f"clients/{client_id}", "-r", "services",
                  "-s", "standardFlowEnabled=false")
            drift = json.dumps({
                "user.attribute": "displayName", "claim.name": "incorrectName",
                "jsonType.label": "String", "id.token.claim": "true",
            })
            admin(compose_file, "update", f"clients/{client_id}/protocol-mappers/models/{mapper_id}",
                  "-r", "services", "-s", f"config={drift}")
        finally:
            reapply(root, compose_file)
        assert client(compose_file)["id"] == client_id, "Client was recreated."
        assert client(compose_file)["standardFlowEnabled"] is True
        after = admin(compose_file, "get", f"clients/{client_id}/protocol-mappers/models", "-r", "services")
        assert isinstance(after, list)
        assert sorted(json.dumps(item, sort_keys=True) for item in after) == sorted(
            json.dumps(item, sort_keys=True) for item in mapper_snapshot
        ), "Repeat apply did not restore mapper settings and preserve IDs."
        browser = KochwikiBrowser(base_url, callback_url)
        code = browser.receive_code(browser.login(email, password), callback)
        status, tokens = token_request(
            token_endpoint, exchange_fields(secret, callback_url, code, browser.verifier)
        )
        assert status == 200, "Configured secret stopped working after reapply."
        refreshed = validate_id_token(tokens, issuer, jwks_url, browser.nonce, email, display_name)
        assert refreshed["sub"] == subject, "User identity changed after reapply."
        print("PASS: repeat apply repairs client/mapper drift and preserves client, user, mapper IDs and secret")
    print(f"Verification completed. One demo account remains: oidc-check-{run}@example.invalid")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify the Kochwiki OIDC client through local hosted login.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--callback-url", default=None)
    parser.add_argument("--compose-file", type=Path, default=Path(__file__).resolve().parents[1]
                        / "deployments/docker-compose-local.yml")
    parser.add_argument("--allow-user-creation", action="store_true")
    arguments = parser.parse_args()
    try:
        verify(arguments.base_url, arguments.compose_file.resolve(), arguments.callback_url,
               arguments.allow_user_creation)
    except (AssertionError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("OIDC verification failed unexpectedly; check local services and configuration.") from None
