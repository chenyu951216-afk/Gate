from fastapi import APIRouter, Request

from app.dependencies import require_bearer, state_from_request
from app.schemas.notification import NotificationTestRequest

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.post("/test")
async def test_notification(request: Request, body: NotificationTestRequest):
    await require_bearer(request)
    return await state_from_request(request).notifier.send_messages([body.message], {"test": True})


@router.get("/history")
async def notification_history(request: Request):
    return {"items": await state_from_request(request).repository.notification_history(100)}

