# Nexus Hosting Options Review (November 27, 2026)

This is a research-only decision note. **No accounts, services, DNS, secrets, or deployments are changed.** The current goal is to keep costs at $0 without relying on a host that quietly turns off or requires a payment card.

## Recommended split

- **Website / dashboard static frontend:** Cloudflare Pages (static assets are the most predictable low-cost part of the stack).
- **Website/API edge:** Cloudflare Workers only for endpoints that fit the Worker runtime and strict CPU/request budgets; the free plan currently allows 100,000 requests/day and 10 ms CPU per invocation. A Worker is not a drop-in always-running Flask process.
- **Discord bot:** Keep it separate from the website runtime. A gateway-connected bot needs a persistent connection, so serverless/sleeping web hosts are not a safe default.
- **Fallback:** Keep Render as a migration rollback target only if its quota is available again; otherwise choose a host based on verified free-tier constraints instead of making a migration solely to chase “free”.

## Candidate comparison

| Provider | $0 tier / current caveat | Fit for Nexus |
|---|---|---|
| Cloudflare Pages + Workers | Static assets are a strong fit. Workers Free has 100k requests/day and 10 ms CPU/invocation; D1/KV also have quotas. Flask must be adapted to the Workers Python runtime/WSGI support and split from bot startup. | **Best for website and eligible API pieces. Not the bot.** |
| Render Free | 750 instance-hours/workspace/month, enough for a single web service; sleeps after 15 idle minutes and can take around a minute to wake. Free Postgres now expires after 30 days; free filesystem is not persistent. | **Keep as a temporary rollback/backup host** if quota is available; not a guaranteed always-on production host. |
| Railway Free | $1 resource credit each month, no card required; up to 1 vCPU / 0.5 GB per service, one project/three services and 0.5 GB volume. Services sleep after 5–10 minutes idle, but outbound activity can keep a service awake. | Useful for testing small web components; **poor bot candidate** because a Discord Gateway connection can keep it active and burn the tiny allowance. |
| PythonAnywhere Beginner | One web app, one worker, 512 MiB disk, restricted outbound Internet, 100 CPU seconds/day for consoles/tasks and a one-month expiry on free web apps. | Not a durable free home for Nexus; outbound restrictions may also block required APIs. |
| Google Cloud Run | Request-based free allowance (monthly) includes 180k vCPU-seconds, 360k GiB-seconds and 2M requests; usage beyond is billed. | Good technical fallback for containerized web/API pieces if billing setup is acceptable and budget alerts/limits are configured. Do not select as a “no-card/no-billing-risk” option. |
| Oracle Cloud Always Free (A1 Arm) | Official Always Free allowance is currently 2 OCPUs + 12 GB RAM total, plus limited x86 Micro VMs. Free VMs can be reclaimed if idle for 7 days. Account signup generally requires phone and credit-card verification. | **Technically the strongest free process-host candidate** for an always-on bot, but unsuitable for our no-card constraint and reclamation risk. Don't pursue without checking signup/card acceptance first. |

## Decision

1. **Proceed with the Cloudflare migration in small PRs** for static website assets and isolate API functionality gradually. Keep Workers/D1 experiments local until tests and architecture review are complete.
2. **Do not move the Discord bot to Cloudflare Workers.** The bot's persistent connection and runtime dependencies need a conventional process host.
3. **No host is selected for the bot yet.** The currently checked free plans either sleep or are too small for a continuously connected bot; Oracle is a better VM candidate but requires payment-card verification and has idle reclamation rules. Continue researching genuinely no-card, always-on alternatives before proposing a migration.
4. **No deploy until explicitly approved.** Every migration step remains a PR with tests; preserve a rollback path and do not touch DNS or production secrets.

## Sources (checked November 27, 2026)

- Cloudflare Workers pricing: https://developers.cloudflare.com/workers/platform/pricing/
- Cloudflare Workers limits: https://developers.cloudflare.com/workers/platform/limits/
- Render Python hosting: https://render.com/hosting/python
- Render Flask hosting: https://render.com/hosting/flask
- Railway Free Plan: https://docs.railway.com/pricing/free-plan
- Railway Pricing: https://railway.com/pricing
- PythonAnywhere pricing: https://www.pythonanywhere.com/pricing/
- PythonAnywhere free-account feature limits: https://help.pythonanywhere.com/pages/FreeAccountsFeatures
- Google Cloud Run pricing: https://cloud.google.com/run/pricing
- Oracle Cloud Always Free resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle Cloud Free Tier account requirements: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm

## Additional no-card, always-on bot candidates (research only)

The following two services currently advertise free always-on Discord bot hosting. They are **unverified by Nexus**; treat the listings as claims, not a reliability guarantee. Before using either, review current terms, outbound networking, privacy/security, support and uptime history, and test with a disposable bot.

| Candidate | Advertised free resources | Notable restrictions / risk |
|---|---|---|
| FadeHost | 256 MB RAM, 0.25 vCPU, 3 GB disk, 24/7, GitHub deploy, no card or renewals | Free app outbound access is restricted to a fixed allowlist; only 1 free app/account; 1 GB/day traffic; no backups included. Verify Discord Gateway + every Nexus API/database endpoint is reachable. |
| Silly Development | 256 MB RAM, 25% CPU, 512 MB disk, 24/7, no card/renewals, Python support | Single operator; modest resource allocation; policy fair-use limits; idle-new-server removal if never used during first 7 days. Consider operational continuity risk. |

### Recommended evaluation order

1. **FadeHost: networking compatibility first.** Its official free-tier documentation says outbound connections are allowlisted. That could be a blocker for Discord Gateway, database access, or any external APIs Nexus needs; confirm each endpoint before considering migration.
2. **Silly Development: resource and continuity check.** Its advertised resources are small and its About page identifies a sole operator. Do not treat 24/7 marketing as a production SLA.
3. **Never transfer production secrets during evaluation.** Use a disposable Discord bot token, no real guild permissions, and a minimal test repo. If the host's network, security, logs, retention, or account deletion controls cannot be validated, reject it.
4. **No migration decision yet.** Keep Render as-is if/when its quota allows, and continue the Cloudflare website migration separately.

### Candidate sources (checked November 27, 2026)

- FadeHost free-plan documentation: https://www.fadehost.com/docs/app-free-plan/
- FadeHost Discord bot hosting documentation: https://www.fadehost.com/docs/discord-bot-hosting/
- Silly Development free-tier page: https://sillydev.co.uk/
- Silly Development Terms: https://sillydev.co.uk/terms
- Silly Development Acceptable Use Policy: https://sillydev.co.uk/acceptable-use
- Silly Development About/operator disclosure: https://sillydev.co.uk/about/
