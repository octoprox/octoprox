# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Needs attention: a project's lines for its Overview card, the mutes its editors set, every project's problem count
for the Projects page and the switcher, and the install-wide lines of the Projects page.

The lines are read from the alerts the workers store (:class:`~api.attention.service.AttentionService`), the install's
too, with the lines the api answers itself when it cannot read them
(:class:`~api.attention.service.InstallAttentionService`).
"""

from fastapi import APIRouter, Request

from api.core.auth import RequireEditorDep
from api.models.attention import InstallAttention, MuteRequest, ProjectAttention, ProjectsAttention
from api.routes.common import actor_of, attention_of, install_attention_of

router = APIRouter()


@router.get("/projects/{project_id}/attention", response_model=ProjectAttention)
async def get_project_attention(request: Request, project_id: str) -> ProjectAttention:
    """The project's lines, muted or not, in display order, with how many problems and warnings show and how many are
    muted, and when the workers last checked it; none for a project pending deletion."""
    return await attention_of(request).project(project_id)


@router.put("/projects/{project_id}/attention/mutes", status_code=204)
async def mute_line(request: Request, project_id: str, body: MuteRequest, user: RequireEditorDep) -> None:
    """Mute one of the project's open alerts now for everyone in it, shown or covered, replacing its mute if it has one;
    recorded in the project's History."""
    await attention_of(request).mute(project_id, body.key, body.duration, actor_of(request, user))


@router.delete("/projects/{project_id}/attention/mutes/{key:path}", status_code=204)
async def unmute_line(request: Request, project_id: str, key: str, user: RequireEditorDep) -> None:
    """Unmute one of the project's lines; nothing when it is not muted, and History records only a mute removed."""
    await attention_of(request).unmute(project_id, key, actor_of(request, user))


@router.get("/attention/projects", response_model=ProjectsAttention)
async def get_projects_attention(request: Request) -> ProjectsAttention:
    """Every project's problem lines whose mute is not active, as its card counts them, oldest project first."""
    return await attention_of(request).problems()


@router.get("/attention/install", response_model=InstallAttention)
async def get_install_attention(request: Request) -> InstallAttention:
    """The install-wide lines, the same for every role: no instance, host, version or raw error; always answered."""
    return await install_attention_of(request).read()
