# GitHub release webhook troubleshooting

Use this checklist when the optional GitHub release webhook does not post an announcement.

## 1. Confirm the endpoint and secret

- The webhook URL must point to your deployed Nexus host and end with `/webhooks/github/releases`.
- GitHub's webhook content type must be `application/json`.
- The webhook secret must exactly match the host's `GITHUB_RELEASE_WEBHOOK_SECRET` environment variable.
- Keep the secret private. If it was exposed, rotate it in both GitHub and the host environment.

## 2. Check GitHub's delivery

In the repository, open **Settings → Webhooks → your webhook → Recent deliveries** and inspect the failed delivery's response code and body. Redeliver only after correcting the issue.

| Response | Meaning / next step |
|---|---|
| `401 Invalid signature` | Secret mismatch, missing `X-Hub-Signature-256`, or payload signature validation failed. Recheck the secret and avoid changing the payload. |
| `503 Webhook is not configured` | Set `GITHUB_RELEASE_WEBHOOK_SECRET` in the deployed environment and restart/redeploy the service. |
| `503 Bot unavailable` | Nexus's Discord connection or database is not ready. Check host logs and retry the delivery when the bot is healthy. |
| `503 Release handler unavailable` | The release-announcement handler is not loaded. Check startup logs and deployment health. |
| `503 Release delivery failed` | Delivery raised an error while forwarding the release. Check Discord channel access and host logs before redelivery. |
| `413 Payload too large` | The request exceeds the receiver's 256 KiB limit. |
| `400` | The delivery ID is missing/invalid or the payload is malformed. Check GitHub's delivery details. |
| `202` or `200 ignored` | The event was validly received but ignored (wrong event/action/repository, draft or prerelease, unconfigured channel, or already announced release). |

## 3. Confirm the event selection

Configure the webhook for **Releases** only. Nexus handles published stable releases; drafts, prereleases, other repositories, and non-published release actions are ignored.

## 4. Confirm Discord configuration

Run `/updates status` in the server that was configured. If no channel is set, use `/updates channel #channel` as the bot owner who also owns that server. Ensure Nexus can view and send messages in the selected text channel.

## 5. Fallback and safe retries

The 10-minute GitHub release poll remains enabled as a fallback. Webhook deliveries are deduplicated by GitHub delivery ID, and release IDs are tracked to avoid repeat announcements. Use `/updates check` to manually publish the latest stable release if needed.

Useful links:
- [GitHub webhook delivery documentation](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks)
- [Nexus release webhook implementation](https://github.com/digravinaloris/Nexus/blob/main/web/github_webhook.py)
- [Nexus release announcement cog](https://github.com/digravinaloris/Nexus/blob/main/cogs/github_updates.py)
