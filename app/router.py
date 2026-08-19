"""Application configuration - root APIRouter.

Defines all FastAPI application endpoints.

Resources:
    1. https://fastapi.tiangolo.com/tutorial/bigger-applications

"""

from fastapi import APIRouter

from app.controllers.v1 import admin, auth, external, llm, saas, track, video

root_api_router = APIRouter()
# v1
root_api_router.include_router(video.router)
root_api_router.include_router(llm.router)
root_api_router.include_router(saas.router)
root_api_router.include_router(auth.router)
root_api_router.include_router(admin.router)
root_api_router.include_router(track.router)
root_api_router.include_router(external.router)
