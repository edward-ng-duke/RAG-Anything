import contextvars, logging, structlog

# Context vars to bind to log records
tenant_id_var = contextvars.ContextVar("tenant_id", default=None)
request_id_var = contextvars.ContextVar("request_id", default=None)
job_id_var = contextvars.ContextVar("job_id", default=None)
# Set by the /v1/onyx/* auth dependency boundary (Task 3.3) so every
# log record emitted while serving an onyx request carries the caller id.
onyx_user_id_var = contextvars.ContextVar("onyx_user_id", default=None)

def _bind_contextvars(_, __, event_dict):
    if (v := tenant_id_var.get()) is not None: event_dict["tenant_id"] = v
    if (v := request_id_var.get()) is not None: event_dict["request_id"] = v
    if (v := job_id_var.get()) is not None: event_dict["job_id"] = v
    if (v := onyx_user_id_var.get()) is not None: event_dict["onyx_user_id"] = v
    return event_dict

def configure_logging(json: bool = True, level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))
    processors = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _bind_contextvars,
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

def get_logger(name: str | None = None):
    return structlog.get_logger(name)
