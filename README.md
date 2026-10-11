# Identity Provider

Local Keycloak 26.8.0 setup for Kochwiki and future applications. Application
accounts belong to the `services` realm; `master` is reserved for administration.
This repository does not yet integrate Kochwiki login or establish a production
deployment.

## Local setup and updates

Copy `.env.example` to `.env` and set bootstrap credentials and the desired port.
Keep credentials outside version control. Docker Compose and PowerShell are
required.

```powershell
docker compose -f docker-compose-local.yml up -d
./scripts/Apply-Configuration.ps1
```

Wait for Keycloak to finish starting before applying configuration. Use the same
apply command after changing the configuration files. It creates a missing realm
or updates the managed settings on an existing realm without recreating it or
deleting users. It uses Keycloak's bundled Admin CLI and fails on unsuccessful
operations. Changes are sequential, not transactional; correct errors and rerun
after a partial failure. Concurrent apply runs are not supported.

The current Compose file uses `start-dev` without a mounted database volume.
Container recreation can lose realm configuration and users. Applying settings
does not recreate the container. Persistent database storage, HTTPS, backups,
restricted network exposure and production administrator credentials remain
separate deployment work. The checked-in port mapping may differ from an already
running demo container; use `docker compose -f docker-compose-local.yml ps` to
inspect the actual published port.

`Apply-Configuration.ps1 -ComposeFile <file> -Service <name>` can target another
Compose deployment. The helper runs inside the Keycloak container and uses
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
configuration deliberately. Users and client registrations are not managed by
this slice. Do not use realm deletion or full replacement imports for updates.

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
the claim in the later integration slice. A shared display-name OIDC mapper is
also deferred until the application client is configured.

References: [realm import limitations](https://www.keycloak.org/server/importExport),
[Admin CLI and realm configuration](https://www.keycloak.org/docs/26.8.0/server_admin/).

## Local verification

With Python 3.10 or later and the demo running, execute:

```powershell
python -u ./tests/verify_local.py --base-url http://localhost:8080 --allow-user-creation
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
