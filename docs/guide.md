# Hybrid Leads: full guide

The short version is in the [README](../README.md). This is the long one: Google Sheet setup, settings, servers, deploys, fixes.

An internal tool for the Hybrid team. It checks public registries every day for new IT company filings (a filing is
any new registration, permit, or license), matches each company to its website, and lists the ones with a website and
contact details (an email, a phone, or both, as set in Settings) in a Google Sheet, ready for mailing. **Preview sheet** shows the real Google Sheet inside the app,
**Download** gives Excel or CSV, and a daily run refreshes everything by itself.

Needs a Mac with Python 3 (if it is missing, `xcode-select --install` provides it) and an internet connection.

## Start it

```
cd ~/it-leads
./it-leads serve --open
```

It opens http://127.0.0.1:8765, a short start page. **Sign in** leads to the form. The whole team uses one login:
the email is `team@hybrid.agency`, and the password is given to the team separately. It is **not written on the
page**, and the start-up message does not print it either (output ends up in log files).

There is no sign-up, and **the code contains no password**. Set the login once:

```
./it-leads set-login --generate      makes a random password and shows it once (or leave --generate off to type one)
./it-leads set-login crew@yourcompany.com --generate      the same, with another team email
```

It is stored in `config.json`, which is never committed to git, and is applied again at every start, so it is always
the real one. Until a password is set nobody can sign in, and the start-up message says so. To change it later, run
the command again and restart; if you change the email, the old login stops working at that start. You can also set
`ITLEADS_LOGIN_EMAIL` and `ITLEADS_LOGIN_PASSWORD` in the environment. To write the login on the sign-in page for
people on a private network, set `"show": true` under `app` > `login` (or `ITLEADS_SHOW_LOGIN=1`).

Then:

1. **Settings > Google Sheet**: copy the script, paste it into Apps Script, deploy it as a Web app, paste the URL
   back (two minutes, once). The sheet is created in your Google Drive. Google warns that it has not verified the
   script (it is your own): Advanced > Go to ... > Allow. The permission text mentions Drive because sharing a file
   needs it; the script only touches the sheet it creates.
2. **Run now.** The first run covers the last 45 days and usually takes under 10 minutes. Later runs take longer on days
   when companies that were held back are checked again, and up to an hour is possible. Until you run once or connect
   Google, the daily schedule stays quiet.

Keep the folder in your home folder (`~/it-leads`), not in Documents or Desktop (macOS blocks background jobs there).

## Keep it running on this Mac

```
./it-leads install-service      starts at login, restarts after a crash
./it-leads uninstall-service
```

The daily run happens inside the app, once a day, at the time set in Settings. If the Mac was asleep or the app was off at that time, it runs as soon as the app is back and the internet is
reachable, and catches up on everything it missed. Companies that were listed but could not reach the sheet (Google
was down) are sent again by themselves, every ten minutes until they arrive.

To update Hybrid Leads, replace the files and run `./it-leads install-service` again (that restarts it). The private
Python environment in `.venv` is rebuilt automatically when `requirements.txt` changes or the folder moves to another
kind of Mac.

## What is in it

- **Start page and sign-in**: what Hybrid Leads is, and the team login.
- **Dashboard**: latest additions, last 7 days, total listed, held back; companies added per day; by registry and
  state; recent runs.
- **Preview sheet**: the Google Sheet in a pop-up (read-only), with an Open in Google Sheets button. If Google is not
  connected, or link sharing is off or refused, it shows the same list in a plain table.
- **Download**: Excel (`.xlsx`, with links) or CSV, with the same company columns as the sheet (14); the sheet also keeps
  Added and ID columns (16 in all).
- **Settings**: Google connection, daily time, what must be known before a company is listed, registries.

If you connect a different sheet later (for example a new script), everything already listed is sent to it.

## What gets listed

A company is listed only when its website is matched to its filing and the contact details Settings require are known: by default an email and a phone, or with "One way to reach them is enough" either one.
The site must carry the company's name, on the page or in its domain, and pass at least one check: the same phone or
street address, its full legal name (a one-word name also needs the filing's city on the same page; on the terms or
contact page, any name also needs the city or ZIP code), the email domain it gave the registry (Connecticut only), or a
domain registered within three weeks of the filing. Two weaker matches are accepted when the site also names the
filing's city: a domain registered between ten months before and two months after the filing, or the company's exact
name as the domain and in the page title, headings, or description. The **Verified by** column shows which check passed. Companies
that miss something are held back and checked again 3, 10, and 30 days after they first appear, then dropped if they
still fall short.

Settings > "What a company needs before it is listed":

- **Phone**: most small company sites do not publish one, so turning it off roughly triples the list. For mailing,
  an email is what counts, so turn it off unless you also call.
- **One way to reach them is enough**: an email or a phone, instead of both.
- **Drop companies whose website shows no sign of IT work** (on by default): the registry's industry code is
  self-reported, and a site with readable text that shows almost no IT work is removed. A site with almost no readable text is kept and
  marked Unverified in the **Fit** column.
- **Skip established companies** (off by default): the website is 3 or more years older than the filing, which
  usually means an existing firm that took out a new permit. When this is off, those companies stay on the list, and
  the **Fit** column says "site since 2001" so you can tell them apart.

A change applies at once to companies already held back.

Privacy: when the filing is a person trading under a business name, only city and state are shown (not a street
address that may be a home) and their license phone is not used. A free-mail address (gmail, yahoo) from a registry is
ignored unless the company's own website shows it. Mailboxes for something other than reaching the company (press,
legal, billing, hiring) are skipped, and so are phone numbers labeled as a press, media, or complaints contact.

## Where the data comes from

| Registry | What it gives | New filings per month (IT industry code) |
|---|---|---|
| Texas Comptroller sales-tax permits | NAICS code, outlet address | about 450 |
| Connecticut business registry | NAICS code, address, email, officers | about 250 |
| San Francisco registered businesses | NAICS code, address; about 4 in 10 are individuals | about 55 |
| Seattle business licenses | NAICS code, address; its phone number is only used to check the website | about 20 |
| Los Angeles active businesses | NAICS code, address | about 5 |

Rounded from September 2026 and the 45 days to 6 Oct 2026; the real numbers move with the registries. Texas loads in
weekly batches and the Los Angeles feed refreshes monthly, so those two arrive in bursts. All are free public records. The industry code is
self-reported, so a few are not IT; the website check removes most. A new license or permit can belong to an older
company opening in that city: look at the registered date.

## Settings you can change in config.json

The Settings page covers the everyday ones. The file `config.json` (created on first start, readable by you only,
it holds the sheet secret and the session secret) also has, under `"app"`:

| Key | Meaning | Default |
|---|---|---|
| `host`, `port` | where the app listens | `127.0.0.1`, `8765` |
| `login` | the team login: `email`, `password`, and `show` (write it on the sign-in page) | set with `./it-leads set-login`, not shown |
| `trusted_proxy` | address of a reverse proxy whose forwarded client address can be believed | none |
| `secure_cookies` | true when the app is served over https | false |
| `allowed_hosts` | extra names the app answers to (a domain in front of a proxy) | none |

Two top-level keys matter for daily use: `schedule` (`hour` and `minute`, also set under Settings > Daily run) and
`after_run`, a command that runs after every run that refreshed the list, for example
`/path/to/it-leads/deploy/vercel/deploy.sh` to keep the Vercel copy current. It is read from `config.json` only (never
from a web page), runs without a shell, one at a time, and its result is written to `logs/after-run.log`.

`ITLEADS_HOST`, `ITLEADS_PORT`, `ITLEADS_TRUSTED_PROXY`, `ITLEADS_SECURE_COOKIES=1`, `ITLEADS_ALLOWED_HOSTS` (names
separated by commas), `ITLEADS_LOGIN_EMAIL`, `ITLEADS_LOGIN_PASSWORD` and `ITLEADS_SHOW_LOGIN=1` do the same from the
shell. `install-service` copies only `ITLEADS_HOST`, `ITLEADS_PORT`, `ITLEADS_TRUSTED_PROXY` and
`ITLEADS_SECURE_COOKIES` into the background service; for the login and `allowed_hosts`, use `config.json`
(`./it-leads set-login` writes the login there). `ITLEADS_HOME=/some/folder` keeps the data and settings elsewhere. Another program on
port 8765? Change `port` here.

## Putting it on a server

```
ITLEADS_HOST=0.0.0.0 ITLEADS_SECURE_COOKIES=1 ITLEADS_LOGIN_PASSWORD='a-long-private-one' ./it-leads serve
```

- Put it behind an HTTPS reverse proxy; secure cookies mark the cookie secure and turn on HSTS. Behind **any** reverse
  proxy, including one on the same machine, set `trusted_proxy` to the proxy's address (`127.0.0.1` for a local one),
  otherwise every visitor looks like the proxy and the per-address sign-in limits lock everybody out together.
- With one shared login, the password is the only gate. Use a long one, keep it off the page (the default), and where
  you can, also restrict who may reach the server (your office addresses, or Cloudflare Access).
- Passwords are stored as salted PBKDF2 hashes, every form and API call carries a CSRF token, failed sign-ins are rate
  limited, pages use a strict content policy, and the config, accounts and database are readable by your user only.
- The app answers only requests addressed to this machine (`127.0.0.1`, `localhost`) unless it is exposed; behind a
  domain, list that name in `allowed_hosts`. That stops a web page on the internet from reaching it through your browser.
- Hybrid Leads fetches company websites itself, directly (not through a system proxy), and refuses to connect to any
  address that points inside a network (localhost, private ranges, link-local), checked at the moment of connecting,
  so a company site cannot aim it at your other machines. Text taken from pages is cleaned before it is stored.
- `install-service` is macOS only (launchd). On Linux run `./it-leads serve` under systemd, for example:

```
[Service]
WorkingDirectory=/home/you/it-leads
ExecStart=/home/you/it-leads/it-leads serve
Environment=ITLEADS_SECURE_COOKIES=1 ITLEADS_TRUSTED_PROXY=127.0.0.1
Restart=on-failure
```

## Deploy it on a server for free (Oracle Cloud Always Free)

A laptop sleeps, loses Wi-Fi and closes its lid: a run started at night once judged most companies "no website"
because the lid was closed (fixed: nothing is judged while the internet is down), but an always-on server avoids the
problem. Hybrid Leads needs a real small computer: a disk that keeps its files (the company list and the accounts live in
SQLite), a background scheduler, and runs that usually take a few minutes and can take up to an hour. That rules out serverless hosts such as
Vercel (no disk, jobs of a few minutes at most, one cron a day on the free plan) and the free tiers of hosts that put
a service to sleep or wipe its disk (Render, Railway, Koyeb).

What works: **Oracle Cloud "Always Free"** (a small ARM server, free with no end date; it asks for a card to check
who you are, and some regions have no free capacity: try another), or any cheap VPS (about 4 dollars a month).

1. Create an Ubuntu server (Oracle: Compute > Instances > Create, shape `VM.Standard.A1.Flex` with 1 core and
   6 GB, a public address). Open ports 80 and 443 in its network rules (Oracle: Networking > your network > Security
   List > add ingress rules for TCP 80 and 443; on the server also run
   `sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT`).
2. Give the server's address a free name, for example at duckdns.org (`yourname.duckdns.org`).
3. On the server: `curl -fsSL https://get.docker.com | sh`, then copy this folder to it (`scp -r` or git).
4. In the folder create `deploy/.env` with these lines (the sign-in is not written on the page; `SHOW_LOGIN=1` would
   write it there, so only use that on a private network):
   ```
   DOMAIN=yourname.duckdns.org
   TZ=Asia/Karachi
   LOGIN_EMAIL=team@hybrid.agency
   LOGIN_PASSWORD=choose-a-long-private-password
   ```
5. `docker compose -f deploy/docker-compose.yml up -d --build`. Https is set up by itself.
6. Open `https://yourname.duckdns.org`, sign in with the email and password from `.env`, then connect Google in
   Settings.

To update: copy the new files and run the same `up -d --build`; the data volume is kept. To look at logs:
`docker compose -f deploy/docker-compose.yml logs -f app`.

## Publish a read-only copy on Vercel

Vercel runs the app as a serverless function: no lasting disk, no background job, so it cannot do the daily search.
What it can do is show the list **as it is now** to people who sign in: the dashboard, the sheet preview, and the Excel
and CSV downloads. Runs and Settings are switched off in that copy (the page says so). Only listed companies are
uploaded with their details; every other company is reduced to a count. The team password is never uploaded: it lives
in Vercel's environment variables.

```
npm i -g vercel && vercel login        one time
deploy/vercel/deploy.sh                builds a fresh snapshot of data/leads.db and publishes it
```

The first time, give the project three variables (Vercel > project > Settings > Environment Variables > Production, or
`vercel env add NAME production` inside `deploy/vercel/dist`): `ITLEADS_LOGIN_EMAIL`, `ITLEADS_LOGIN_PASSWORD` and
`ITLEADS_SECRET_KEY` (any long random text; it keeps a sign-in valid on every serverless instance). Without the
password variable the copy refuses to start. After each `./it-leads run`, run `deploy/vercel/deploy.sh` again to refresh
the copy. For a copy that searches and updates by itself, use the always-on server above (Docker) instead.
Vercel's free Hobby plan is for non-commercial use; an agency tool belongs on a Pro team.

## Other commands

```
./it-leads status            what happened lately
./it-leads run               fetch now without the web app (also works from cron: ./it-leads run --scheduled)
./it-leads doctor            check every registry, Google, and the service
./it-leads open              open the app in your browser (it must be running)
./it-leads export            CSV of the list (--incomplete for the held-back ones, --out FILE)
./it-leads set-login [EMAIL] [--generate]    set the team email and password (stored in config.json)
./it-leads reset-password EMAIL    lift a sign-in lockout
```

## If something goes wrong

- *"Google answered with a web page"*: in Apps Script, Deploy > Manage deployments, set access to **Anyone**. Some
  company Google accounts do not allow that; use a personal one.
- *"Google would not let this sheet be shared by link"*: your Google account's rules forbid it (common on work or
  school accounts). Everything still works; the in-app preview shows a plain table and the sheet opens from your Drive.
- *Google only offers "Anyone within your organization" when deploying*: Hybrid Leads runs outside your organization and
  cannot reach it. Create the script from a personal Google account instead.
- *Script version or secret mismatch*: Settings > Reconnect > copy the script again, paste it, then Deploy > Manage
  deployments > pencil > New version (not "New deployment": that changes the URL).
- *"Another program is using that port"*: set another `port` in config.json. *"It is already running"*: it is; open
  the address shown.
- *The service does not start*: read `logs/web.out.log` and `logs/web.err.log`; macOS may be waiting for you to allow it
  under System Settings > General > Login Items & Extensions.
- *Locked out* (too many wrong passwords): the limit lifts by itself within 15 minutes, or run
  `./it-leads reset-password EMAIL` to lift it now. For the team email that is all it does (the team password comes from
  `set-login`); for any other account it also asks for a new password.
- *A registry does not resolve*: some internet providers fail on a few government sites (data.texas.gov on one network).
  Hybrid Leads falls back to a public DNS lookup for those; `./it-leads doctor` shows which.
- Logs are in `logs/`; the data is in `data/`. Hybrid Leads never deletes a company row from your sheet's Companies tab;
  only the Dashboard tab is cleared and rewritten after each run.

## The icon and logo

All in `itleads/web/static/`. `favicon.svg`, `favicon-32.png`, `favicon-48.png`, `apple-touch-icon.png`, `icon-192.png` and
`icon-512.png` are the lime tile from the Hybrid logo (navy and white mark). `hybrid-logo.png` is the white web logo
from hybridmediaworks.com, used on the dark pages. `mark.svg` is the large mark behind the sign-in form, traced from
the official favicon as a vector. To use other artwork, replace a file under the same name.

## Use it responsibly

The records are public. Email and phone numbers are business contact details found on company websites, and domain
dates come from public domain records. Before you send cold email or call, check the rules: CAN-SPAM (your postal
address in every email, a working unsubscribe link, opt-outs honored within 10 business days), state telemarketing
laws, and the Do Not Call registry. This is a reminder, not legal advice.
