from dataclasses import dataclass

DEFAULT_EGRESS_HOSTS = frozenset({"fonts.googleapis.com", "fonts.gstatic.com"})


@dataclass(frozen=True)
class RenderConfig:
    concurrency: int = 12                  # parallel Chromium pages (PriorityGate capacity)
    recycle_max_renders: int = 300         # recycle the browser after this many renders
    recycle_max_age_minutes: int = 30      # ... or after this many minutes
    timing_logs: bool = False              # per-phase render timing at INFO level
    allowed_egress_hosts: frozenset[str] = DEFAULT_EGRESS_HOSTS
    pdf_producer: str | None = None        # None -> f"sheetrender/{__version__}"


_config: RenderConfig | None = None


def configure(config: RenderConfig) -> None:
    """Configure rendering. Call before the first render for predictable behavior."""
    global _config
    _config = config


def get_config() -> RenderConfig:
    """Return the configured rendering options, or their defaults."""
    return _config if _config is not None else RenderConfig()
