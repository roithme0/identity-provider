#!/usr/bin/env bash
set -euo pipefail

config_dir="${1:?Supply the configuration directory}"
umask 077
client_file="$(mktemp /tmp/identity-provider-client.XXXXXX)"
trap ': > "$client_file"' EXIT
cat > "$client_file"
if [[ ! -s "$client_file" ]]; then
  echo 'Rendered Kochwiki client configuration is required on stdin.' >&2
  exit 1
fi
server="${IDP_ADMIN_SERVER:-http://localhost:8080}"
admin_realm="${IDP_ADMIN_REALM:-master}"
admin_user="${IDP_ADMIN_USERNAME:-${KC_BOOTSTRAP_ADMIN_USERNAME:-}}"
export KC_CLI_PASSWORD="${IDP_ADMIN_PASSWORD:-${KC_BOOTSTRAP_ADMIN_PASSWORD:-}}"
if [[ -z "$admin_user" || -z "$KC_CLI_PASSWORD" ]]; then
  echo 'Administrator credentials are required.' >&2
  exit 1
fi

kcadm() {
  /opt/keycloak/bin/kcadm.sh "$@" --no-config --server "$server" \
    --realm "$admin_realm" --user "$admin_user"
}

realms="$(kcadm get realms --fields realm)"
if [[ "$realms" =~ \"realm\"[[:space:]]*:[[:space:]]*\"services\" ]]; then
  kcadm update realms/services -f "$config_dir/services-realm.json"
  echo 'Updated services realm settings.'
else
  kcadm create realms -f "$config_dir/services-realm.json"
  echo 'Created services realm.'
fi

kcadm update users/profile -r services -f "$config_dir/services-user-profile.json"
for action in VERIFY_EMAIL UPDATE_EMAIL; do
  kcadm update "authentication/required-actions/$action" -r services \
    -s enabled=false -s defaultAction=false
done
echo 'Applied services user profile and disabled email required actions.'
client_id="$(kcadm get clients -r services -q clientId=kochwiki --fields id --format csv --noquotes)"
if [[ -z "$client_id" ]]; then
  kcadm create clients -r services -f "$client_file" >/dev/null
  client_id="$(kcadm get clients -r services -q clientId=kochwiki --fields id --format csv --noquotes)"
elif [[ "$client_id" == *$'\n'* ]]; then
  echo 'Expected exactly one Kochwiki client.' >&2
  exit 1
else
  kcadm update "clients/$client_id" -r services -f "$client_file" >/dev/null
fi
if [[ ! "$client_id" =~ ^[[:xdigit:]-]{36}$ ]]; then
  echo 'Cannot resolve the Kochwiki client identifier.' >&2
  exit 1
fi
for mapper_file in "$config_dir"/kochwiki/kochwiki-mapper-*.json; do
  mapper_name="$(basename "$mapper_file" .json)"
  mapper_id=''
  mapper_count=0
  mapper_rows="$(kcadm get "clients/$client_id/protocol-mappers/models" -r services --fields id,name --format csv --noquotes)"
  while IFS=, read -r existing_id existing_name; do
    if [[ "$existing_name" == "$mapper_name" ]]; then
      mapper_id="$existing_id"
      mapper_count=$((mapper_count + 1))
    fi
  done <<< "$mapper_rows"
  if (( mapper_count > 1 )); then
    echo 'Duplicate managed claim mapper; resolve the conflict before applying.' >&2
    exit 1
  fi
  if [[ -z "$mapper_id" ]]; then
    kcadm create "clients/$client_id/protocol-mappers/models" -r services -f "$mapper_file" >/dev/null
  else
    kcadm update "clients/$client_id/protocol-mappers/models/$mapper_id" -r services -f "$mapper_file" >/dev/null
  fi
done
echo 'Applied Kochwiki OIDC client and identity claim mappers.'
echo 'Configuration applied. Existing users, client identifiers and unmanaged settings are preserved.'
