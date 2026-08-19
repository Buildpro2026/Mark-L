# Moving JARVIS to Oracle Cloud Always Free

This is a runbook, not a record of something already done. As of this
phase, no Oracle Cloud account, credentials, or CLI access exist
anywhere in this environment, so the actual provisioning steps below
have not been executed. Everything here is what actually needs to
happen, in order, once real Oracle access exists. Render stays
production until every step below is done and verified.

## What to provision

Oracle's Always Free tier includes, at time of writing, either:

- Up to 4 Ampere A1 (ARM) OCPUs and 24 GB RAM, split across up to 4
  instances, or
- Two AMD-based VM.Standard.E2.1.Micro instances (1 OCPU, 1 GB RAM each)

For JARVIS: pick **one Ampere A1 instance with 2 OCPUs / 12 GB RAM**.
Playwright's Chromium alone needs real memory headroom, and the AMD
Micro shape's 1 GB is too tight once the agent scheduler, Chromium, and
Python are all running together. Ampere A1 is ARM64 - the Dockerfile in
this repo builds fine on ARM (python:3.12-slim and Playwright both ship
ARM64 images), so this is not a blocker, just something to be aware of
if testing the image locally on an x86 machine first (use
`docker buildx build --platform linux/arm64` to cross-check).

Also provision:
- A block volume or use the boot volume for `/app/data` (Always Free
  includes up to 200 GB total block storage - a few GB is enough for
  `jarvis2.db`, logs, and `long_term.json`)
- A public IP (included)
- A domain name pointed at that IP (not Oracle's job - use whatever
  registrar/DNS Lee already has, or a free option like a subdomain)

## Networking

Oracle's default security list blocks inbound traffic by default. Open:
- Port 22 (SSH) - restrict the source CIDR to Lee's actual IP if possible,
  not 0.0.0.0/0
- Port 80 (HTTP) - needed for Let's Encrypt's certificate challenge
- Port 443 (HTTPS) - the real public entry point

Port 8787 (the app's own port) should **not** be opened to the internet
directly - only Caddy (running on the same host, reached via the
container network) should talk to it. This mirrors the same
never-expose-the-raw-service pattern the existing JARVIS_API_TOKEN
bearer-auth model already assumes.

## Getting the code and secrets onto the instance

1. `git clone` this repository onto the instance (or pull, if re-deploying).
2. Install Docker and the Docker Compose plugin (Oracle's Ubuntu/Oracle
   Linux images both support the standard Docker install instructions).
3. Create a `.env` file **directly on the instance** (never commit it,
   never copy it through a channel that logs it) with the same variable
   names `docker-compose.yml` references: `JARVIS_API_TOKEN`,
   `GEMINI_API_KEY`, `ANTHROPIC_TOKEN`, `HUBSPOT_TOKEN`, `BUFFER_TOKEN`,
   `AIRTABLE_TOKEN`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
   `TWILIO_FROM_NUMBER`, `GOOGLE_TOKEN_JSON`, `GOOGLE_CLIENT_SECRET_JSON`,
   `JARVIS_OBSIDIAN_VAULT_PATH`. Pull the actual values from Render's
   dashboard (Environment tab) or from the local machine's `.env` -
   never print them into a terminal that gets logged/screen-recorded.
4. Edit `Caddyfile` and replace `your-domain.example.com` with the real
   domain.
5. `docker compose up -d --build`

## Restart behavior

`restart: unless-stopped` on both services means Docker restarts them
automatically if the process crashes or the VM reboots, without manual
intervention - matches Render's own auto-restart behavior. To confirm
this actually works, reboot the instance (`sudo reboot`) after the
first successful deploy and verify `/health` responds again within a
minute or two without anyone running a command.

## Verification checklist (run this before touching DNS/cutover)

Against `https://<oracle-ip-or-domain>` directly (bypassing any DNS
cutover) or after pointing a *test* subdomain at it:

- [ ] `/health` returns `status: ok`
- [ ] `/`, `/3d`, `/login`, `/ui` all return 200
- [ ] Logging in via `/ui/login` with the real token works and the
      session survives a container restart (`docker compose restart jarvis`)
- [ ] `/api/tools` and `/api/orchestrator/summary` work with the bearer
      token, and fail with 401 without it
- [ ] A real chat turn through `/ui/api/chat` returns a normal-length
      response (see docs/PERFORMANCE section on expected latency)
- [ ] `data/jarvis2.db` persists across `docker compose down && docker
      compose up -d` (the named volume, not the container, is what
      makes this true - confirm a row written before `down` is still
      there after `up`)
- [ ] Gmail/Calendar/HubSpot tool calls succeed with the same
      credentials that work on Render

## Cutover

Only after every box above is checked, repeatedly, over more than one
day of real use:

1. Point the real production domain's DNS at the Oracle instance.
2. Update any hardcoded references to the old Render URL (`actions/
   cloud_bridge.py`'s `JARVIS_CLOUD_URL` default, the desktop
   `.env`'s `JARVIS_CLOUD_URL` if set) to the new domain.
3. Leave the Render service running and paid/free-tier as-is for at
   least a week of parallel operation before considering shutting it
   down. Do not delete it. If Oracle has a problem, flipping
   `JARVIS_CLOUD_URL` back and re-pointing DNS is the rollback, and it
   only works if Render is still there to fall back to.
4. Only after Oracle has been the sole production target for a
   sustained period with no incidents should Render actually be
   decommissioned - and that is a decision for Lee to make
   deliberately, not something to automate.
