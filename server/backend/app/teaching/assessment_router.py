from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from ..planning.outputs import OUTLINE_LIMITS
from .graphics import DrawingAnswer


class GenerateIn(BaseModel):
    idempotency_key: str = Field(min_length=1,max_length=100)


class SubmitIn(GenerateIn):
    # A transport guard based on the existing whole-course capacity, not a
    # 36-question business limit. Actual count must match the published unit.
    answers: dict[str, list[str] | str | DrawingAnswer] = Field(max_length=12 * OUTLINE_LIMITS["max_lessons"])


def create_assessment_router(service,resolve_user):
    router=APIRouter(tags=["unit-assessments"])

    @router.get("/api/courses/{course_id}/units/{unit_id}/scope")
    def scope(course_id: str, unit_id: str, request: Request):
        return service.scopes.get(course_id, unit_id, resolve_user(request).user_id)

    @router.post("/api/courses/{course_id}/units/{unit_id}/scope", status_code=202)
    def generate_scope(course_id: str, unit_id: str, request: Request):
        return service.scopes.generate(course_id, unit_id, resolve_user(request).user_id)

    @router.post("/api/courses/{course_id}/units/{unit_id}/scope/retry", status_code=202)
    def retry_scope(course_id: str, unit_id: str, request: Request):
        return service.scopes.retry(course_id, unit_id, resolve_user(request).user_id)

    @router.get("/api/courses/{course_id}/units/{unit_id}/assessment")
    def unit(course_id:str,unit_id:str,request:Request):
        return service.unit_status(course_id,unit_id,resolve_user(request).user_id)

    @router.post("/api/courses/{course_id}/units/{unit_id}/assessment",status_code=202)
    def generate(course_id:str,unit_id:str,body:GenerateIn,request:Request):
        return service.generate(course_id,unit_id,resolve_user(request).user_id,body.idempotency_key)

    @router.get("/api/assessments/{assessment_id}")
    def get(assessment_id:str,request:Request):
        return service.get(assessment_id,resolve_user(request).user_id)

    @router.post("/api/assessments/{assessment_id}/attempts",status_code=202)
    def submit(assessment_id:str,body:SubmitIn,request:Request):
        return service.submit(assessment_id,resolve_user(request).user_id,body.answers,body.idempotency_key)

    @router.get("/api/assessment-attempts/{attempt_id}")
    def attempt(attempt_id:str,request:Request):
        return service.attempt(attempt_id,resolve_user(request).user_id)

    @router.post("/api/assessment-tasks/{task_id}/retry",status_code=202)
    def retry(task_id:str,request:Request):
        return service.retry(task_id,resolve_user(request).user_id)

    return router
