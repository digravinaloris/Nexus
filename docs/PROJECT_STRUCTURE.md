# Project structure

Nexus keeps its Discord bot entrypoint at the repository root while implementation modules live in focused packages.

## Main packages

| Path | Responsibility |
|---|---|
| `main.py` | Discord bot entrypoint and startup orchestration |
| `nexus_runtime/` | Shared process state, bot reference, and MongoDB collection registry |
| `cogs/` | Discord commands and event handlers |
| `services/` | Bot-backed operations, including server backup snapshots and safe restoration, shared by Discord commands and the in-process web adapter |
| `security/` | Turnstile integration and member verification flows |
| `stores/` | MongoDB persistence and data-access functions |
| `web/` | Flask app factory, routes, dashboard, API, and bot-service adapters |
| `catalogs/` | Shared command metadata and feature-toggle definitions |
| `audit_services/` | Audit journal queries and database-backed exports |
| `audit_helpers/` | Audit visualization and export formatting helpers |
| `calendar_helpers/` | Calendar parsing, timezone, and date utilities |
| `helpers/` | Dependency-light shared utilities |
| `localization/` | Translation loading and language/theme helpers |
| `nexus_logging/` | Structured JSON logging |
| `locales/` | English and French YAML translations |
| `templates/` | Flask/Jinja templates |
| `assets/` | Static images and other application assets |
| `tests/` | Automated tests and architecture guards |

## Legacy compatibility modules

The `compat/` package contains small re-export modules for integrations that still use the former module interfaces. Production code must import canonical modules directly from their owning packages; `compat/` is reserved for migration and legacy callers.

**New code should import from the owning package**, for example:

```python
from nexus_runtime import core
from services.discord_logging import log_dashboard_action
from stores.config_store import get_config
from security.verification import create_challenge
from helpers.utils import parse_duration
```

Do not add new implementation logic to compatibility aliases. Keep `main.py` as the root entrypoint unless the deployment/startup contract is deliberately changed and covered by tests.
