#!/usr/bin/env bash
#
# RETIRED 2026-06-12 by the Chatwoot-native rehaul (PR #102).
#
# This script used to register the Meta app's `instagram` webhook subscription
# so Instagram-Login DMs were delivered to meta_router
# (POST /{app}/subscriptions → callback https://api.getpolaris.my/v1/webhooks/meta).
#
# That inbound path NO LONGER EXISTS: meta_router.py is deleted, and inbound for
# every Meta channel now arrives via Chatwoot natively (Meta → Chatwoot →
# agent-bot webhook → Polaris). Re-pointing Instagram at /v1/webhooks/meta now
# would deliver IG DMs to a 404 — the exact "no messages" symptom this script
# was originally written to fix.
#
# The Instagram subscription is now configured MANUALLY as part of the cutover,
# pointing at Chatwoot (callback https://chat.getpolaris.my/webhooks/instagram,
# verify token = Chatwoot GlobalConfig INSTAGRAM_VERIFY_TOKEN). See:
#   ../docs/internal-ops/runbooks/chatwoot-native-cutover.md  §3 Instagram
#
# Kept as an inert stub (not deleted) so the vps-install.sh call site does not
# dangle. Exits 0 so a deploy never aborts here.
#
# Usage:  ./register-meta-webhooks.sh   (no-op)
set -euo pipefail

printf '\n\033[1;33m! register-meta-webhooks.sh is RETIRED (Chatwoot-native rehaul).\033[0m\n'
printf '  Instagram inbound is now handled by Chatwoot, not meta_router.\n'
printf '  Configure the IG webhook subscription per the cutover runbook:\n'
printf '    ../docs/internal-ops/runbooks/chatwoot-native-cutover.md  (§3 Instagram)\n\n'
exit 0
