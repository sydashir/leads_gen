#!/bin/bash
# Publish a read-only copy of Hybrid Leads on Vercel (the list as it is now; it cannot run or refresh itself).
#   one time:   npm i -g vercel && vercel login
#   each time:  deploy/vercel/deploy.sh          (then reload the site)
# The first deployment also needs three environment variables, see "Publish a read-only copy on Vercel" in docs/guide.md.
set -e
cd "$(dirname "$0")/../.."
.venv/bin/python deploy/vercel/build_bundle.py
cd deploy/vercel/dist
vercel link --yes --project "${VERCEL_PROJECT:-hybrid-leads}" >/dev/null     # the folder is rebuilt every time, so link again
vercel deploy --prod --yes "$@"
