"""HTTP fixtures verify actual adapters, units, pagination and bounded failure categories."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from trader.infra.http_contracts import HttpResponse

NOW = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


class Session:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(kwargs)
        return HttpResponse(self.status, json.dumps(self.payload).encode())

    get = post

    def execute(self, request):
        response = self.get(
            request.url, timeout=request.timeout_seconds, deadline=request.deadline,
            max_response_bytes=request.max_response_bytes, cancel_requested=request.cancel_requested,
            io_timeout_seconds=request.io_timeout_seconds,
        )
        if request.receive_headers is not None:
            request.receive_headers(response.status_code, response.header_items)
            request.receive_chunk(response.content)
        return response
