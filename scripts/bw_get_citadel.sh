#!/usr/bin/env bash
set -euo pipefail

ITEM_NAME="${CITADEL_BW_ITEM_NAME:-loansphereservicingdigital.bkiconnect.com}"
ITEM_ID="${CITADEL_BW_ITEM_ID:-}"
ITEM_URI_HOST="${CITADEL_BW_URI_HOST:-loansphereservicingdigital.bkiconnect.com}"
ITEM_LOGIN_HINT="${CITADEL_BW_LOGIN_HINT:-coolwoodllc}"
ITEM_SEARCH_NAMES="${CITADEL_BW_SEARCH_NAMES:-$ITEM_NAME coolwoodllc Citadel}"
EXPECTED_ITEM_ID="${CITADEL_BW_EXPECTED_ITEM_ID:-}"
EXPECTED_ORGANIZATION_ID="${CITADEL_BW_EXPECTED_ORGANIZATION_ID:-}"
EXPECTED_COLLECTION_ID="${CITADEL_BW_EXPECTED_COLLECTION_ID:-}"
EXPECTED_FOLDER_ID="${CITADEL_BW_EXPECTED_FOLDER_ID:-}"
EXPECTED_FOLDER_NAME="${CITADEL_BW_EXPECTED_FOLDER_NAME:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
OPENCLAW_ROOT="${OPENCLAW_ROOT:-$(cd "$ROOT/.." && pwd)}"

if ! command -v bw >/dev/null 2>&1; then
  echo "bw CLI not found. Install with: npm install -g @bitwarden/cli" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found. Install with: sudo apt-get install -y jq" >&2
  exit 1
fi

BW_ENV="${BW_ENV:-$ROOT/.secrets/bw.env}"
BW_ENV_FALLBACK="${BW_ENV_FALLBACK:-$OPENCLAW_ROOT/.env}"
ENSURE_SCRIPT="${BW_ENSURE_SCRIPT:-$ROOT/scripts/bw_ensure_session.sh}"
export BW_NOINTERACTION="${BW_NOINTERACTION:-true}"

if [[ -f "$BW_ENV_FALLBACK" ]]; then
  _fallback_session="$(set -a; source "$BW_ENV_FALLBACK" 2>/dev/null; printf '%s' "${BW_SESSION:-}")"
  if [[ -n "$_fallback_session" ]] && BW_NOINTERACTION=true bw unlock --check --session "$_fallback_session" >/dev/null 2>&1; then
    BW_SESSION="$_fallback_session"
  fi
  unset _fallback_session
fi

if [[ -z "${BW_SESSION:-}" ]]; then
  if [[ -x "$ENSURE_SCRIPT" ]]; then
    BW_ENV="$BW_ENV" "$ENSURE_SCRIPT" >/dev/null
  fi

  # shellcheck disable=SC1090
  set -a; source "$BW_ENV" 2>/dev/null || true; set +a
fi

if [[ -z "${BW_SESSION:-}" ]]; then
  echo "BW_SESSION unavailable after ensure step" >&2
  exit 1
fi

if ! BW_NOINTERACTION=true bw unlock --check --session "$BW_SESSION" >/dev/null 2>&1; then
  echo "BW_SESSION is unavailable or locked" >&2
  exit 1
fi

if [[ -z "$EXPECTED_FOLDER_ID" && -n "$EXPECTED_FOLDER_NAME" ]]; then
  EXPECTED_FOLDER_ID="$(
    BW_NOINTERACTION=true bw list folders --session "$BW_SESSION" 2>/dev/null \
      | jq -r --arg folder_name "${EXPECTED_FOLDER_NAME,,}" '
          [.[] | select((.name // "" | ascii_downcase) == $folder_name) | .id // ""] | first // ""
        '
  )"
  if [[ -z "$EXPECTED_FOLDER_ID" ]]; then
    echo "Unable to resolve Citadel Bitwarden folder from CITADEL_BW_EXPECTED_FOLDER_NAME" >&2
    exit 1
  fi
fi

resolve_item() {
  if [[ -n "$ITEM_ID" ]]; then
    BW_NOINTERACTION=true bw get item "$ITEM_ID" --session "$BW_SESSION" 2>/dev/null
    return
  fi

  local search_json="[]"
  local direct_json
  if direct_json="$(BW_NOINTERACTION=true bw get item "$ITEM_NAME" --session "$BW_SESSION" 2>/dev/null)"; then
    if jq -e \
      --arg host "${ITEM_URI_HOST,,}" \
      --arg login_hint "${ITEM_LOGIN_HINT,,}" \
      --arg expected_item_id "$EXPECTED_ITEM_ID" \
      --arg expected_organization_id "$EXPECTED_ORGANIZATION_ID" \
      --arg expected_collection_id "$EXPECTED_COLLECTION_ID" \
      --arg expected_folder_id "$EXPECTED_FOLDER_ID" '
      def has_login: ((.login.username // "") != "" and (.login.password // "") != "");
      def uri_matches:
        if $host == "" then true
        else ([.login.uris[]?.uri // "" | ascii_downcase | contains($host)] | any)
        end;
      def login_hint_matches:
        if $login_hint == "" then true
        else (
          ((.login.username // "" | ascii_downcase) == $login_hint)
          or ((.name // "" | ascii_downcase) == $login_hint)
          or ([.fields[]? | ((.name // "" | ascii_downcase) == $login_hint) or ((.value // "" | ascii_downcase) == $login_hint)] | any)
        )
        end;
      def guard_matches:
        (($expected_item_id == "") or ((.id // "") == $expected_item_id))
        and (($expected_organization_id == "") or ((.organizationId // "") == $expected_organization_id))
        and (($expected_collection_id == "") or ([.collectionIds[]? | tostring] | index($expected_collection_id) != null))
        and (($expected_folder_id == "") or ((.folderId // "") == $expected_folder_id));
      has_login and uri_matches and login_hint_matches and guard_matches
    ' >/dev/null <<<"$direct_json"; then
      search_json="$(jq -c -n --argjson found "$direct_json" '[$found]')"
    fi
  fi

  local term term_json
  for term in $ITEM_SEARCH_NAMES; do
    term_json="$(BW_NOINTERACTION=true bw list items --search "$term" --session "$BW_SESSION" 2>/dev/null || printf '[]')"
    search_json="$(jq -c -n --argjson existing "$search_json" --argjson found "$term_json" '$existing + $found | unique_by(.id // .name)')"
  done
  jq -e \
    --arg needle "${ITEM_NAME,,}" \
    --arg host "${ITEM_URI_HOST,,}" \
    --arg login_hint "${ITEM_LOGIN_HINT,,}" \
    --arg expected_item_id "$EXPECTED_ITEM_ID" \
    --arg expected_organization_id "$EXPECTED_ORGANIZATION_ID" \
    --arg expected_collection_id "$EXPECTED_COLLECTION_ID" \
    --arg expected_folder_id "$EXPECTED_FOLDER_ID" '
    def newest($items; $tier):
      ($items | sort_by(.revisionDate // .creationDate // "") | last)
      + {
        _citadel_resolution: {
          selected_by: "newest_revision",
          tier: $tier,
          match_count: ($items | length),
          selected_revisionDate: ((($items | sort_by(.revisionDate // .creationDate // "") | last) // {}).revisionDate // null)
        }
      };
    def has_login: ((.login.username // "") != "" and (.login.password // "") != "");
    def uri_matches:
      if $host == "" then false
      else ([.login.uris[]?.uri // "" | ascii_downcase | contains($host)] | any)
      end;
    def login_hint_matches:
      if $login_hint == "" then false
      else (
        ((.login.username // "" | ascii_downcase) == $login_hint)
        or ((.name // "" | ascii_downcase) == $login_hint)
        or ([.fields[]? | ((.name // "" | ascii_downcase) == $login_hint) or ((.value // "" | ascii_downcase) == $login_hint)] | any)
      )
      end;
    def text_matches:
      ((.name // "" | ascii_downcase) == $needle)
      or ((.login.username // "" | ascii_downcase) == $needle)
      or ([.fields[]? | ((.name // "" | ascii_downcase) == $needle) or ((.value // "" | ascii_downcase) == $needle)] | any);
    def guard_matches:
      (($expected_item_id == "") or ((.id // "") == $expected_item_id))
      and (($expected_organization_id == "") or ((.organizationId // "") == $expected_organization_id))
      and (($expected_collection_id == "") or ([.collectionIds[]? | tostring] | index($expected_collection_id) != null))
      and (($expected_folder_id == "") or ((.folderId // "") == $expected_folder_id));
    . as $items
    | [ $items[] | select(has_login and (uri_matches or text_matches) and guard_matches) ] as $matches
    | [ $matches[] | select(uri_matches and login_hint_matches) ] as $host_login_matches
    | [ $matches[] | select(login_hint_matches) ] as $login_matches
    | [ $matches[] | select(uri_matches) ] as $host_matches
    | if ($host_login_matches | length) >= 1 then newest($host_login_matches; "host_login")
      elif ($login_matches | length) >= 1 then newest($login_matches; "login")
      elif ($host_matches | length) >= 1 then newest($host_matches; "host")
      elif ($matches | length) >= 1 then newest($matches; "text")
      else empty
      end
  ' <<<"$search_json"
}

bw_get_item() {
  local item_json
  if ! item_json="$(resolve_item)"; then
    echo "Unable to resolve Citadel Bitwarden item from CITADEL_BW_ITEM_NAME/CITADEL_BW_ITEM_ID" >&2
    return 1
  fi
  local guarded_json
  if ! guarded_json="$(jq -e \
    --arg expected_item_id "$EXPECTED_ITEM_ID" \
    --arg expected_organization_id "$EXPECTED_ORGANIZATION_ID" \
    --arg expected_collection_id "$EXPECTED_COLLECTION_ID" \
    --arg expected_folder_id "$EXPECTED_FOLDER_ID" \
    --arg expected_folder_name "$EXPECTED_FOLDER_NAME" '
      def expected_fields:
        [
          (if $expected_item_id != "" then "item_id" else empty end),
          (if $expected_organization_id != "" then "organization_id" else empty end),
          (if $expected_collection_id != "" then "collection_id" else empty end),
          (if $expected_folder_id != "" then "folder_id" else empty end),
          (if $expected_folder_name != "" then "folder_name" else empty end)
        ];
      def matches_guard:
        (($expected_item_id == "") or ((.id // "") == $expected_item_id))
        and (($expected_organization_id == "") or ((.organizationId // "") == $expected_organization_id))
        and (($expected_collection_id == "") or ([.collectionIds[]? | tostring] | index($expected_collection_id) != null))
        and (($expected_folder_id == "") or ((.folderId // "") == $expected_folder_id));
      if matches_guard then
        . + {
          _citadel_resolution: (
            (._citadel_resolution // {})
            + {
              guard_checked: true,
              guard_configured: ((expected_fields | length) > 0),
              guard_expected_fields: expected_fields,
              guard_match: true
            }
          )
        }
      else empty
      end
    ' <<<"$item_json")"; then
    echo "Resolved Citadel Bitwarden item did not match CITADEL_BW_EXPECTED_* guard" >&2
    return 1
  fi
  printf '%s\n' "$guarded_json"
}

FIELD="${1:-json}"
case "$FIELD" in
  username)
    bw_get_item | jq -r '.login.username'
    ;;
  password)
    bw_get_item | jq -r '.login.password'
    ;;
  json)
    bw_get_item
    ;;
  *)
    echo "Usage: $0 [username|password|json]" >&2
    exit 2
    ;;
esac
