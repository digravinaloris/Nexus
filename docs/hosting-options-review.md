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

## Decision

1. **Proceed with the Cloudflare migration in small PRs** for static website assets and isolate API functionality gradually. Keep Workers/D1 experiments local until tests and architecture review are complete.
2. **Do not move the Discord bot to Cloudflare Workers.** The bot's persistent Discord connection and runtime dependencies need a conventional process host.
3. **No host is selected for the bot yet.** Research more candidates (including genuinely free or low-cost process/VPS options) and check account/card requirements before proposing any migration.
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
