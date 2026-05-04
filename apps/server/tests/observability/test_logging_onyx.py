import logging
import io
import pytest
import structlog

from rag_service.observability.logging import (
    configure_logging,
    onyx_user_id_var,
    request_id_var,
    tenant_id_var,
    job_id_var,
)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Reset all contextvars before and after each test (they persist across tests)."""
    onyx_user_id_var.set(None)
    tenant_id_var.set(None)
    request_id_var.set(None)
    job_id_var.set(None)
    yield
    onyx_user_id_var.set(None)
    tenant_id_var.set(None)
    request_id_var.set(None)
    job_id_var.set(None)


def _capture():
    """Capture structlog output to a string buffer."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    logger = logging.getLogger()
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    return buf


def test_onyx_user_id_var_default_is_none():
    assert onyx_user_id_var.get() is None


def test_log_record_includes_onyx_user_id_when_set():
    configure_logging(json=True, level="INFO")
    buf = _capture()
    onyx_user_id_var.set("u_alice")
    structlog.get_logger().info("hello")
    record = buf.getvalue().strip()
    assert "u_alice" in record
    assert "onyx_user_id" in record


def test_log_record_excludes_onyx_user_id_when_unset():
    configure_logging(json=True, level="INFO")
    buf = _capture()
    onyx_user_id_var.set(None)
    structlog.get_logger().info("hi")
    record = buf.getvalue().strip()
    assert "onyx_user_id" not in record


def test_other_contextvars_still_bound_when_present():
    configure_logging(json=True, level="INFO")
    buf = _capture()
    tenant_id_var.set("onyx-test")
    request_id_var.set("req_abc")
    onyx_user_id_var.set("u_alice")
    structlog.get_logger().info("multi")
    record = buf.getvalue().strip()
    assert "onyx-test" in record
    assert "req_abc" in record
    assert "u_alice" in record
