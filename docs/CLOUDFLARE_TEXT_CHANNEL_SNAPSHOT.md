# Cloudflare text-channel snapshot contract

Status: the signed bot-host dispatch endpoint now has a narrowly scoped read-only operation. The Worker Fetch client does not expose this operation yet; that is a separate follow-up step. This change does not enable the endpoint or deploy any Cloudflare resources.

## Operation

- Method: `api_text_channels_snapshot`
- Arguments: exactly one decimal Discord guild ID string, 1–20 digits.
- Result: either `null` when the bot runtime is unavailable, or a list of at most 500 channel DTOs.
- DTO shape: exactly `{"id": "<decimal channel id>", "name": "<channel name>"}`.
- Channel names must be non-empty strings of at most 100 characters.
- Extra fields, malformed IDs, oversized lists, invalid names and non-list results fail closed with HTTP 503.
- Invalid arguments are rejected before nonce consumption and before calling the BotService.

The operation reuses the existing `InProcessBotService.api_text_channels_snapshot` implementation. It returns only text-channel IDs and names; no Discord objects, permissions, message contents or mutations cross the boundary.

## Security and rollout

- The operation is added to the existing signed `POST /internal/v1/dispatch` allowlist only.
- Existing HMAC verification, timestamp checks, durable nonce replay protection, body limits and disabled-by-default registration remain unchanged.
- The endpoint is not exposed through the public mobile API.
- The Cloudflare Worker client must add matching argument/result validation and local Wrangler smoke coverage before a Worker route can use this operation.
- No production environment variables, secrets, DNS records, Cloudflare resources or deployments are changed.
