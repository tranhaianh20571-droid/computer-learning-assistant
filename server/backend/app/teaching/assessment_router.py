from fastapi import APIRouter, Request
from pydantic import BaseModel, Field


class GenerateIn(BaseModel):
    idempotency_key: str = Field(min_length=1,max_length=100)


class SubmitIn(GenerateIn):
    answers: dict[str, list[str] | str] = Field(max_length=8)


def create_assessment_router(service,resolve_user):
    router=APIRouter(tags=["unit-assessments"])

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
