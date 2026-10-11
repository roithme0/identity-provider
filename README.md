# Identity Provider

Keycloak 26.8.0 deployments for Kochwiki and future applications. Application
accounts belong to the `services` realm; `master` is reserved for administration.
This repository does not yet integrate Kochwiki login. Staging and production
deployment definitions are HTTP-only preparation, not a public production rollout.

## Local setup and updates

Copy `deployments/.env.example` to `deployments/.env` and set bootstrap credentials,
the desired port, database name (`KC_DB_NAME`), database user (`KC_DB_USER`),
and two different random database passwords: `POSTGRES_PASSWORD`
(database initialization administrator) and `KC_DB_PASSWORD` (Keycloak database owner).
Keep credentials outside version control. Docker Compose and PowerShell are
required.

```powershell
docker compose --env-file deployments/.env -f deployments/docker-compose-local.yml up -d
./scripts/Apply-Configuration.ps1
```

Wait for Keycloak to finish starting before applying configuration. Use the same
apply command after changing the configuration files. It creates a missing realm
or updates the managed settings on an existing realm without recreating it or
deleting users. It uses Keycloak's bundled Admin CLI and fails on unsuccessful
operations. Changes are sequential, not transactional; correct errors and rerun
after a partial failure. Concurrent apply runs are not supported.

The local deployment uses `start-dev`. Nginx is the only service with a published
host port, configured through `BIND_ADDRESS` and `BIND_PORT` (localhost by default). The example environment uses port 8006; open
`http://localhost:8006/realms/services/account/` to sign in or register.
Use your configured port if it differs. Local uses the same fixed hostname and explicit HTTP settings as staging/production; consistently access the URL from `KC_HOSTNAME` rather than switching between localhost and its IP address.

## Staging and production preparation

All Compose files use `deployments/.env`, copied from the single `.env.example`.
Set credentials, `BIND_PORT`, `BIND_ADDRESS` and `KC_HOSTNAME` for the deployment.
`KC_HOSTNAME` is the full external URL, including scheme and nonstandard port;
the example uses `http://localhost:8006`. `BIND_ADDRESS` defaults to localhost.
Each deployed repository copy supplies its own ignored `.env` and independent
secrets. To run multiple environments from one checkout, override ports and
hostname explicitly; the shared file otherwise gives them the same values.

```powershell
docker compose --env-file deployments/.env -f deployments/docker-compose-staging.yml up -d
./scripts/Apply-Configuration.ps1 -ComposeFile deployments/docker-compose-staging.yml

docker compose --env-file deployments/.env -f deployments/docker-compose-prod.yml up -d
./scripts/Apply-Configuration.ps1 -ComposeFile deployments/docker-compose-prod.yml
```

Staging and production use `start`, fixed hostname configuration and
`restart: unless-stopped` for all services. Each project has independent networks
and its own `postgres-data` volume. The same initialization and realm configuration
files are used in every environment. Do not override project names to the same
value or share database volumes between environments.

All three deployments include Nginx, Keycloak and PostgreSQL. Only Nginx publishes
a port. Nginx overwrites forwarded client/protocol headers; Keycloak reads
`X-Forwarded-*`. Docker DNS resolution allows Nginx to find a replacement Keycloak
container without retaining its old address. PostgreSQL is on a separate internal
network, joined only by Keycloak. Database and Keycloak management ports are not
published. The admin console is reachable through the proxy alongside login;
separate administrative access restrictions remain future deployment work.

Certificates and HTTPS are deliberately deferred. These configurations do not
provide encrypted login and must not be used for public authentication yet.
Realm SSL requirements remain unchanged: non-local HTTP login may be rejected.
Before public deployment, configure HTTPS and certificate renewal in Nginx,
update `KC_HOSTNAME` to the stable HTTPS URL, and validate redirects, cookies,
proxy headers and administrative access. Do not merely change the hostname to
HTTPS while Nginx is still serving HTTP. Backups remain deferred.

## Database persistence

A dedicated `postgres:16` service stores Keycloak realms, users, credentials,
clients and other database state in the `postgres-data` named volume. Compose
prefixes its name with the project name (`identity-provider-local_postgres-data`).
The database has no published host port and is reached over the Docker network.
Keycloak starts after the PostgreSQL health check succeeds; wait for Keycloak
itself to finish starting before using the apply command.

The PostgreSQL image creates the database using `POSTGRES_DB`, populated from
`KC_DB_NAME`. Then `deployments/postgres/init-keycloak.sh` creates the restricted
`KC_DB_USER` role and transfers database ownership to it. The role has no
superuser, database-creation or role-creation privileges.
The separate PostgreSQL administrator is used for initialization; Keycloak
connects using its own database credentials. Keycloak manages its schema.
Initialization runs only when the volume is empty. Changing the database name or
user in `.env` does not rename an existing database or role; those changes require
a coordinated database migration. Editing database passwords
in `.env` does not change existing database credentials; credential rotation
requires an explicit database change and coordinated application update.

Ordinary container replacement retains the volume and its data. Keep the Compose
project name stable so it continues using the same volume. Do not delete that
volume when replacing containers. Changing the PostgreSQL major version requires
a planned database upgrade; changing the image tag alone is insufficient.
The former embedded-database demo accounts were intentionally not migrated.
Backups and recovery are deferred; this volume does not protect against disk
failure or accidental volume deletion.

`Apply-Configuration.ps1 -ComposeFile <file> -Service <name>` can target another
Compose deployment. Relative paths are resolved from the repository root.
The wrapper uses the adjacent `.env` for every Compose file, independently of the working directory.
`-EnvFile <path>` overrides this selection; selected
environment files must exist. The helper runs inside the Keycloak container and uses
`IDP_ADMIN_SERVER` (default `http://localhost:8080`), `IDP_ADMIN_REALM` (default
`master`), `IDP_ADMIN_USERNAME` and `IDP_ADMIN_PASSWORD` from its environment.
For the local demo, credentials fall back to the container's bootstrap variables.
Those variables do not change an existing administrator's password. Production
must provide suitable deployment credentials separately; do not put credentials
on command lines or enable shell tracing. Admin CLI authentication is transient;
the helper does not persist tokens to a CLI configuration file.

## Managed configuration

- `config/services-realm.json`: self-registration, email/password login, password
  policy, SMTP disabled, and temporary brute-force lockouts.
- `config/services-user-profile.json`: the complete managed profile schema.
  Registration collects email, password and required non-unique `displayName`.
  First and last name are omitted. Keycloak derives the internal username from
  email. Display names retain spaces and capitalization, reject control
  characters, and require 1-50 characters measured after trimming.
- `scripts/apply-config.sh`: realm create/update, profile configuration and
  disabling `VERIFY_EMAIL` and `UPDATE_EMAIL` required actions.

Realm updates send only explicitly managed fields, preserving other realm
settings. The user-profile schema is managed as a whole: add future attributes
to its configuration file before applying. SMTP and the email required actions
are explicitly disabled on every apply; future mail support must change that
configuration deliberately. Users and other client registrations are not managed
by this configuration. Do not use realm deletion or full replacement imports for updates.

Passwords require at least 15 characters, with spaces/passphrases allowed, no
composition rules, no periodic expiry and no blocklist. Temporary lockouts use
the version's defaults: 30 failures, Multiple escalation, 60-second increment,
900-second maximum wait, 43200-second failure reset, 1000-millisecond quick-login
check and 60-second quick-login wait. Permanent lockouts are disabled. Review
these explicit defaults when upgrading Keycloak.

Email verification and email password recovery are disabled. Email ownership is
not established; another person can register an address first. Forgotten
passwords require administrator-assisted recovery, whose ownership checks still
need to be defined. Application account linkage must use validated OIDC issuer
and subject, never email or display name.

Keycloak's length validator trims for validation; trimming persisted profile
values is not assumed. Kochwiki must normalize the display name when consuming
the claim in the later integration slice. The Kochwiki client now includes a
display-name ID-token mapper.

References: [realm import limitations](https://www.keycloak.org/server/importExport),
[Admin CLI and realm configuration](https://www.keycloak.org/docs/26.8.0/server_admin/).

## Kochwiki OIDC client

The managed confidential client `kochwiki` uses Authorization Code flow with
mandatory PKCE `S256` and backend client-secret authentication. Direct Access
Grants, Implicit Flow, service accounts, device authorization and CIBA are
disabled. Browser token requests and CORS origins are not configured: the future
FastAPI integration owns code exchange. No Kochwiki endpoints or sessions are
implemented by this repository.

Set these values in the selected deployment's ignored `.env` before applying:

- `KOCHWIKI_CLIENT_SECRET`: a different random secret per environment, at least
  32 characters without whitespace. The example placeholder is rejected. The
  backend will later receive the same value through its own environment.
- `KOCHWIKI_REDIRECT_URIS`: space-separated exact callback URLs, without wildcards.
  The local example allows `http://localhost:8000/api/auth/callback` and
  `http://localhost:4200/api/auth/callback`.
- `KOCHWIKI_LOGOUT_REDIRECT_URIS`: space-separated exact logout return URLs.
  The local example uses `/signed-out` on those two origins.
- `KOCHWIKI_TEST_CALLBACK_URL`: one local test callback, default
  `http://localhost:8765/callback`. Only local Compose includes this additional
  redirect. Staging and production ignore this variable.

For staging/production, replace the example application URL lists with their
HTTPS equivalents. The apply wrapper rejects non-HTTPS application URLs there;
local HTTP is accepted only on loopback hosts. It also rejects URL credentials,
queries, fragments and wildcards. Certificates for the provider remain deferred.

The Compose `x-kochwiki` extension resolves the client values through normal
Compose environment interpolation. It is metadata for the apply wrapper, not
container configuration; the secret is not added to Nginx, PostgreSQL or Keycloak
container environment variables. Updating these client values requires applying
configuration, not restarting containers. The wrapper sends rendered client JSON
over stdin; the helper uses a private temporary file inside Keycloak and clears
its contents on exit. Never print full resolved Compose configuration, enable
shell tracing, or commit `.env`.

`config/kochwiki/kochwiki-client.json` defines fixed settings.
`config/kochwiki/kochwiki-mapper-*.json` defines three ID-token-only mappers for `email`,
`email_verified` and `displayName`. Keycloak supplies standard `iss`, `sub`,
audience, expiry and other protocol claims. New clients start without additional
client scopes; existing scope assignments and unrelated mappers remain unmanaged.
The mapper values retain provider whitespace, so Kochwiki must trim display names.
Email remains unverified initially.

Repeat apply updates the existing client and managed mappers by their identifiers,
without recreating users or clients or generating a new secret. The configured
secret is reapplied unchanged. Changing it deliberately rotates it and requires
a coordinated backend update. Apply remains sequential and nontransactional.

### Local OIDC verification

The stand-in callback verifies the provider configuration before Kochwiki's
backend integration exists. It binds only to `127.0.0.1` and exists only while
the test runs. It automates the hosted login forms; it does not test browser
rendering or implement application sessions.

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r tests/requirements.txt
./.venv/Scripts/python.exe -u tests/verify_oidc.py --allow-user-creation
```

The tool reads the selected local Compose configuration in memory, without
printing secrets. It uses `KC_HOSTNAME` and `KOCHWIKI_TEST_CALLBACK_URL` by default.
If the callback port is occupied, change its environment value and apply before
rerunning. Optional `--base-url` and `--callback-url` must match the canonical
issuer and allowed callback. The test dependency PyJWT includes cryptographic
signature verification.

Checks cover exact client configuration, registration/login, a real local
callback with state verification, code exchange, RS256 signature, issuer,
audience, expiry, nonce and profile claims. Missing PKCE, unregistered callbacks,
incorrect client secrets, incorrect PKCE verifiers and code reuse are rejected.
It temporarily disables Standard Flow and changes the display-name mapper, then
reapplies in a `finally` block and verifies settings, client/user/mapper IDs and
the configured secret still work. If interrupted during that check, rerun the
apply script. One uniquely named test account remains; generated account
passwords, authorization codes and tokens are not printed or persisted.

## Local verification

With Python 3.10 or later and the demo running, execute:

```powershell
python -u ./tests/verify_local.py --base-url http://localhost:8006 --allow-user-creation
```

Use the port actually published by your container. The verification uses only
Python's standard library and is restricted to localhost. Its HTTP cookie client
accepts Secure cookies locally to exercise Keycloak's hosted forms without
changing server security settings. It is not a browser rendering test.

The checks cover registration fields, invalid passwords and display names,
unverified immediate access, duplicate email rejection, duplicate display-name
acceptance, email/password login, and a repeat apply that corrects a deliberately
changed password-length setting while preserving users and clients. The test
temporarily changes the minimum length to 16 before applying the configured 15;
if interrupted at that point, run the apply command to restore configuration.
It creates two uniquely named demo accounts, does not delete them, and prints
their common prefix. Their random passwords are not printed or persisted.
Lockout parameters are verified by readback, not by exercising 30 failed logins.

Persistence verification deliberately replaces both local containers while
retaining their database volume and creates one remaining test account:

```powershell
python -u ./tests/verify_persistence.py --base-url http://localhost:8006 --allow-recreate
```

This temporarily interrupts local sign-in. It verifies that both container IDs
change, the database volume is reused, and realm settings, profile configuration,
client IDs, user ID, display name and password login survive without reapplying
configuration. Passwords are generated in memory and are not printed or saved.
