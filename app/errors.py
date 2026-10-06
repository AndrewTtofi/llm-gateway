"""Errors in OpenAI's shape, so SDK clients raise the right exception class."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi.responses import JSONResponse

from app.providers import UnsupportedProvider
from app.providers.base import (
    CLIENT_FAULT_STATUS,
    QUOTA_CODES,
    NotConfigured,
    ProviderError,
    UnsupportedRequest,
)
from app.routing.router import AllTargetsFailed


def error_response(
    status: int,
    message: str,
    type_: str = "invalid_request_error",
    code: str | int | None = None,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "param": param, "code": code}},
        headers=headers,
    )


def provider_error_response(exc: ProviderError) -> JSONResponse:
    """Map an upstream failure to what the client should see.

    Client-caused upstream 4xx (bad params, context too long) pass through. Anything that
    is the gateway's problem — provider auth, wrong configured model (404), exhausted
    quota, provider outage — is a 5xx, so clients don't "fix" a request that was fine.
    """
    status = exc.status
    if exc.timeout:
        return error_response(504, exc.message, "api_error", "upstream_timeout")
    if exc.code in QUOTA_CODES:
        # Not retryable: waiting won't help, so no retry-after and not a 429.
        return error_response(503, exc.message, "api_error", "upstream_quota_exhausted")
    if status == 429:
        return error_response(
            429, exc.message, "rate_limit_error", "upstream_rate_limited", headers=exc.headers
        )
    if status in CLIENT_FAULT_STATUS:
        return error_response(status, exc.message, "invalid_request_error", "upstream_rejected")
    return error_response(502, exc.message, "api_error", "upstream_error")


def routing_error_response(exc: AllTargetsFailed) -> JSONResponse:
    headers = exc.routed.headers()
    if exc.all_open:
        return error_response(
            503,
            "all providers for this model are temporarily unavailable",
            "api_error",
            "all_providers_unavailable",
            headers=headers,
        )
    last = exc.last
    if isinstance(last, UnsupportedRequest):
        return error_response(400, str(last), code="unsupported_parameter", headers=headers)
    if isinstance(last, NotConfigured):
        # Operator problem (missing API keys). Generic: don't tell clients env var names.
        return error_response(
            503,
            "no provider for this model is currently available",
            "api_error",
            "all_providers_unavailable",
            headers=headers,
        )
    if isinstance(last, UnsupportedProvider):
        return error_response(
            501, str(last), "api_error", "provider_not_supported", headers=headers
        )
    if isinstance(last, ProviderError):
        resp = provider_error_response(last)
        resp.headers.update(headers)
        return resp
    return error_response(
        503,
        "no provider could serve the request",
        "api_error",
        "all_providers_unavailable",
        headers=headers,
    )
