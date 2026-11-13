"""HTTP routes of the folders of the private cloud (presentation layer).

    POST /api/folders                  create a folder              -> 201 Created
    GET  /api/folders?parent_id=...    sub-folders of a folder (root if absent)
    GET  /api/folders/{id}             one folder and its path (breadcrumb)

The files of a folder are listed by ``GET /api/files?folder_id=...`` and
uploaded into it by ``POST /api/files?folder_id=...`` (app/routers/files.py).

Folders are purely logical (database only), so these routes never touch the
disk. Like every route, they act on behalf of ``get_current_user_id()``, and
another user's folder answers 404 exactly like a missing one.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.database import get_session
from app.dependencies import get_folder_service
from app.schemas import FolderCreate, FolderDetail, FolderOut, FolderRef
from app.services.file_policy import InvalidFolderNameError
from app.services.folder_service import (
    FolderNameTakenError,
    FolderNotFoundError,
    FolderService,
    FolderTooDeepError,
)

router = APIRouter(prefix="/api/folders", tags=["folders"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=FolderOut,
    summary="Create a folder in the private cloud",
    responses={
        404: {"description": "Parent folder not found (or owned by another user)"},
        409: {"description": "A folder with this name already exists here (on_conflict = fail)"},
        422: {"description": "Invalid name, or nested deeper than the limit"},
    },
)
async def create_folder(
    body: FolderCreate,
    response: Response,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: FolderService = Depends(get_folder_service),
) -> FolderOut:
    try:
        folder = await service.create(
            session, user_id, body.name, body.parent_id, on_conflict=body.on_conflict
        )
    except (InvalidFolderNameError, FolderTooDeepError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except FolderNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found."
        ) from None
    except FolderNameTakenError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A folder with this name already exists here."
        ) from None
    # REST: a 201 Created response points to the new resource.
    response.headers["Location"] = f"/api/folders/{folder.id}"
    return folder


@router.get(
    "",
    response_model=list[FolderOut],
    responses={404: {"description": "Parent folder not found (or owned by another user)"}},
)
async def list_folders(
    parent_id: uuid.UUID | None = Query(default=None, description="Folder to list; absent = the root."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: FolderService = Depends(get_folder_service),
):
    """Sub-folders of ``parent_id`` (the root if absent), sorted by name."""
    try:
        return await service.list_children(session, user_id, parent_id)
    except FolderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.") from None


@router.get(
    "/{folder_id}",
    response_model=FolderDetail,
    responses={404: {"description": "Folder not found (or owned by another user)"}},
)
async def get_folder(
    # Typed as a UUID: any other value is rejected with 422 before this runs.
    folder_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: FolderService = Depends(get_folder_service),
) -> FolderDetail:
    """One folder and its path from the root (for the breadcrumb)."""
    try:
        view = await service.get(session, user_id, folder_id)
    except FolderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.") from None
    folder = view.folder
    return FolderDetail(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        created_at=folder.created_at,
        path=[FolderRef(id=step_id, name=step_name) for step_id, step_name in view.path],
    )
