"""Authorized command proxy. The web database remains strictly read-only."""
from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.commands import forward_clear, validate_command


class ClearBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    alarm_event_id: int = Field(gt=0, lt=2**63, strict=True)


def add_command_routes(app, settings):
    @app.post('/api/console/nodes/{dev_eui}/clear-alarm')
    def clear_alarm(dev_eui: str, body: ClearBody, request: Request,
                    authorization: str | None = Header(default=None)):
        if not settings.token:
            raise HTTPException(503, 'Manual clearing is disabled; configure the operator token in web and service.')
        if not settings.authorized(authorization):
            raise HTTPException(401, 'Operator authorization required')
        # No cookie authentication. Reject cross-site requests even with a token.
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Cross-origin commands are not allowed')
        if request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none'):
            raise HTTPException(403, 'Cross-site commands are not allowed')
        # Never send administrative credentials over plaintext on a remote LAN.
        if request.url.scheme != 'https' and request.client.host not in ('127.0.0.1', '::1', 'localhost'):
            raise HTTPException(403, 'Remote operator commands require HTTPS')
        try:
            node, alarm_id = validate_command({'dev_eui': dev_eui, 'alarm_event_id': body.alarm_event_id})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        status, result = forward_clear(settings, node, alarm_id)
        return JSONResponse(result, status_code=status, headers={'Cache-Control': 'no-store'})
