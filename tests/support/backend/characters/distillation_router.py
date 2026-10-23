"""Authenticated character creation and resumable build projections."""
import asyncio
import json
from time import monotonic

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .distillation_models import BuildRequest
from .service import reject


class BuildAction(BaseModel):
    selected: str = Field(default="",max_length=120)


def create_distillation_router(service,resolve_user):
    router=APIRouter(prefix="/api/character-builds",tags=["characters"])
    @router.post("/material")
    async def material(request:Request,file:UploadFile=File(...)):
        resolve_user(request)
        try:
            payload=await file.read(10_000_001)
            from .dialogue import extract_material
            return await run_in_threadpool(extract_material,file.filename,payload)
        finally:
            await file.close()
    @router.post("",status_code=202)
    async def create(request:Request):
        raw=await request.body()
        if len(raw)>1_200_000:reject("invalid_field",413)
        try:parsed=BuildRequest.model_validate_json(raw)
        except ValueError:reject("invalid_field",422)
        return await run_in_threadpool(service.create,resolve_user(request).user_id,parsed)
    @router.get("")
    def listing(request:Request):
        from sqlalchemy import select
        from .distillation_models import CharacterBuildRow
        owner=resolve_user(request).user_id
        with service.db.session() as sess:
            ids=list(sess.scalars(select(CharacterBuildRow.build_id).where(
                CharacterBuildRow.owner_user_id==owner).order_by(CharacterBuildRow.created_at.desc()).limit(30)))
        items=[]
        for ident in ids:
            try:items.append(service.get(owner,ident))
            except Exception as exc:
                if getattr(exc,"status",0)!=404:raise
        return {"data":items}
    @router.get("/{build_id}")
    def get(build_id:str,request:Request):
        return service.get(resolve_user(request).user_id,build_id)
    @router.get("/{build_id}/content-events")
    async def content_events(build_id:str,request:Request):
        from .dialogue_stream import dialogue_snapshot
        owner=resolve_user(request).user_id
        initial=await run_in_threadpool(dialogue_snapshot,service,owner,build_id)
        async def events():
            snapshot,previous=initial,None
            heartbeat=monotonic()
            while not await request.is_disconnected():
                if snapshot!=previous:
                    yield "event: "+snapshot["event"]+"\ndata: "+json.dumps(snapshot,ensure_ascii=False)+"\n\n"
                    previous=snapshot
                if snapshot["event"]=="done":
                    return
                if monotonic()-heartbeat>=10:
                    yield ": heartbeat\n\n"
                    heartbeat=monotonic()
                await asyncio.sleep(.12)
                try:
                    snapshot=await run_in_threadpool(dialogue_snapshot,service,owner,build_id)
                except Exception as exc:
                    data={"event":"error","error_code":service._code(exc)}
                    yield "event: error\ndata: "+json.dumps(data)+"\n\n"
                    return
        return StreamingResponse(events(),media_type="text/event-stream",
            headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})
    @router.post("/{build_id}/accept")
    def accept(build_id:str,request:Request):
        return service.accept(resolve_user(request).user_id,build_id)
    @router.post("/{build_id}/{action}")
    def action(build_id:str,action:str,request:Request,body:BuildAction):
        owner=resolve_user(request).user_id
        if action=="select":
            data=service.action(owner,build_id,action,body.selected)
            return service.create(owner,BuildRequest.model_validate(data))
        return service.action(owner,build_id,action)
    return router
