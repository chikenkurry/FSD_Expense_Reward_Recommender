# Deployment

The Docker image is non-root, multi-stage, and exposes a local writable `/data` volume. Set `CARD_ADMIN_BEARER` and `CARD_INTERNAL_BEARER` at startup; no secret has a default. `compose.yaml` is deliberately a local single-volume demonstration and not a production scaling claim.

Production requires a selectable server database implementation before replicas, load balancing, or Kubernetes deployment. Until then, deploy one API and one worker sharing a local SQLite volume only where filesystem locking is guaranteed. Probes use `/health`; readiness means SQLite schema initialization succeeds. Avoid logs containing authorization headers, raw documents, query strings, or tokens.
