# Hybrid Leads

Checks public business registries every day (Texas, Connecticut, Seattle, San Francisco, Los Angeles) for new IT
company filings, matches each company to its website, and lists the ones with an email or a phone: in a dashboard,
a Google Sheet, and Excel/CSV downloads. An internal tool of Hybrid Mediaworks.

## Run it

Needs a Mac with Python 3.

```
./it-leads set-login --generate     # once: makes the team password and shows it
./it-leads serve --open             # opens http://127.0.0.1:8765
```

Sign in with `team@hybrid.agency` and that password, connect Google under Settings (one time), press **Run now**.
Once it has run (or Google is connected) it checks by itself once a day, at the time set in Settings;
`./it-leads install-service` keeps it running on a Mac.

## Other commands

```
./it-leads run                fetch and look up now, without the web app
./it-leads status             what happened lately
./it-leads export             CSV of the list
./it-leads doctor             check the registries, Google, and the service
./it-leads install-service    keep it running on this Mac
```

## Deploy

- Always-on server: Docker + Caddy, see `deploy/`.
- Read-only copy on Vercel: `deploy/vercel/deploy.sh`.

More detail in [docs/guide.md](docs/guide.md). Tests: `python -m unittest discover -s tests`.

## Use it responsibly

The records are public business data. If you email or call, follow CAN-SPAM, state telemarketing laws, and the
Do Not Call registry, and honor opt-outs. This is a reminder, not legal advice.
