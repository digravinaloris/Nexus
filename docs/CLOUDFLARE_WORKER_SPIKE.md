## Runtime configuration boundary

PR #228 added the optional `environ` mapping to `create_web_app()` while preserving process-environment defaults. PR #229 adds a test for configuration precedence: values supplied through `environ` populate Flask defaults, then explicit `config` overrides those values. This makes configuration deterministic in tests and future runtime adapters; it does not prove that the production route surface runs inside Cloudflare Workers.


For a Worker integration, runtime bindings and secrets must be mapped deliberately, and Worker-specific bindings/transport must be passed into adapters rather than relying on process environment or importing bot startup modules. Deployment, Cloudflare account resources, remote D1, DNS, and production cutover remain outside this spike.
