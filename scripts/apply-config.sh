#!/usr/bin/env bash
set -euo pipefail

config_dir="${1:?Supply the configuration directory}"
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
echo 'Configuration applied. Existing users and unmanaged realm settings are preserved.'
