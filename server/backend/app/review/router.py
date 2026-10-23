from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    course_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", max_length=80)


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(strict=True, ge=1)


class AnswerIn(RevisionIn):
    answer: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=100)
    rating: int = Field(strict=True, ge=2, le=4)


class NextIn(RevisionIn):
    skip: bool = False


class DraftIn(RevisionIn):
    answer: str = Field(max_length=4000)
    rating: int = Field(strict=True, ge=2, le=4)


def create_review_router(service, resolve_user):
    router = APIRouter(tags=["review"])

    def owner(request):
        return resolve_user(request).user_id

    @router.get("/api/review/overview")
    def overview(request: Request, timezone: str = "Asia/Shanghai"):
        return service.overview(owner(request), timezone)

    @router.get("/api/review/courses/{course_id}/points")
    def points(course_id: str, request: Request, timezone: str = "Asia/Shanghai"):
        return service.points(owner(request), course_id, timezone)

    @router.post("/api/review/sessions", status_code=202)
    def create(body: CreateIn, request: Request):
        return service.create(owner(request), body.course_id, body.idempotency_key, body.timezone)

    @router.get("/api/review/sessions/{ident}")
    def get(ident: str, request: Request):
        return service.get(ident, owner(request))

    @router.post("/api/review/sessions/{ident}/answers", status_code=202)
    def answer(ident: str, body: AnswerIn, request: Request):
        return service.answer(ident, owner(request), body)

    @router.post("/api/review/sessions/{ident}/help")
    def help_point(ident: str, body: RevisionIn, request: Request):
        return service.move(ident, owner(request), body.revision, help_only=True)

    @router.post("/api/review/sessions/{ident}/next")
    def next_point(ident: str, body: NextIn, request: Request):
        return service.move(ident, owner(request), body.revision, skip=body.skip)

    @router.post("/api/review/sessions/{ident}/retry", status_code=202)
    def retry(ident: str, request: Request):
        return service.retry(ident, owner(request))

    @router.put("/api/review/sessions/{ident}/draft")
    def draft(ident: str, body: DraftIn, request: Request):
        return service.draft(ident, owner(request), body)

    @router.post("/api/review/sessions/{ident}/cancel")
    def cancel(ident: str, request: Request):
        view = service.get(ident, owner(request))
        return service.tasks.request_cancel(view["task_id"], actor_id=owner(request))

    return router
