import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.exceptions import ScannerError
from app.lifespan import lifespan


app = FastAPI(
    title="Gate 永續合約量化找幣、排名與歷史重播回測系統",
    version="1.0.0",
    description="Gate USDT 永續合約找幣、條件下單、分層止盈止損與持倉管理服務。",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"code": "validation_error", "message": "request validation failed", "request_id": request.state.request_id, "details": exc.errors()})


@app.exception_handler(ScannerError)
async def scanner_error(request: Request, exc: ScannerError):
    return JSONResponse(status_code=502, content={"code": type(exc).__name__, "message": str(exc), "request_id": request.state.request_id, "details": {}})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"code": "internal_error", "message": "internal server error", "request_id": request.state.request_id, "details": {}})
