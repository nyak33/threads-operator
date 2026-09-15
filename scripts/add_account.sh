#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <account-key>" >&2
    exit 2
fi

ACCOUNT_KEY="$1"
if [[ ! "$ACCOUNT_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; then
    echo "Invalid account key. Use letters, digits, '-' or '_' only." >&2
    exit 2
fi

OPERATOR_HOME="${THREADS_OPERATOR_HOME:-$HOME/.threads-operator}"
ACCOUNTS_DIR="$OPERATOR_HOME/accounts"
PROFILE_DIR="$OPERATOR_HOME/browser-profiles/$ACCOUNT_KEY"
ENV_FILE="$ACCOUNTS_DIR/$ACCOUNT_KEY.env"

mkdir -p "$ACCOUNTS_DIR" "$PROFILE_DIR"
chmod 700 "$OPERATOR_HOME" "$ACCOUNTS_DIR" "$OPERATOR_HOME/browser-profiles" "$PROFILE_DIR"

if [[ -e "$ENV_FILE" ]]; then
    echo "Account config already exists: $ENV_FILE" >&2
    exit 3
fi

cat > "$ENV_FILE" <<EOF
THREADS_ACCESS_TOKEN=replace_me
THREADS_USER_ID=replace_me
THREADS_API_BASE_URL=https://graph.threads.net/v1.0
SUPABASE_URL=https://replace-me.supabase.co
SUPABASE_SERVICE_ROLE_KEY=replace_me
THREADS_ACCOUNT_SAMPLE_MINUTES=15
THREADS_BROWSER_PROFILE=$PROFILE_DIR
ACTIVITY_FOLLOW_COLLECTOR_ENABLED=false
THREADS_POSTING_ENABLED=false
THREADS_EXECUTION_MODE=approval_required
THREADS_QUEUE_TABLE=threads_publish_queue
THREADS_QUEUE_CAMPAIGN_CODE=
EOF
chmod 600 "$ENV_FILE"

echo "Created $ENV_FILE"
echo "Created browser profile directory $PROFILE_DIR"
echo "Fill in the local credentials, establish the browser login if Activity is needed, then run:"
echo "threads-operator doctor --account $ACCOUNT_KEY"
