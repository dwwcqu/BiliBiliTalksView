"""Safe JSON preserves protocol strings, including escaped surrogate code points."""

import json

from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse


class APIError(Exception):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


class SafeJSONResponse(JSONResponse):
    def render(self, content):
        return json.dumps(
            jsonable_encoder(content), ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")


def response(data, status=200, headers=None):
    return SafeJSONResponse(data, status_code=status, headers=headers)
