import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status, Header, Request, Query, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database import get_db
from app.security import hash_password, verify_password, create_access_token
from app.dependencies import get_current_user, require_admin, enforce_user_access
from app.utils import parse_tus_metadata
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/uploads")
TEMP_DIR = os.path.join(UPLOAD_DIR, "tus_chunks")
FINAL_DIR = os.path.join(UPLOAD_DIR, "completed")

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(FINAL_DIR, exist_ok=True)

TUS_VERSION = "1.0.0"
CHUNK_SIZE = 1024 * 1024 * 5  # 5MB chunk buffer for streaming I/O

app = FastAPI(
    title="Cloud Storage API (tus Resumable Upload Engine)",
    version="1.0.0"
)


# -----------------------------------------------------------------------------
# PUBLIC ENDPOINTS
# -----------------------------------------------------------------------------
@app.get("/public/health")
def health_check():
    return {"status": "online"}

@app.post("/public/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    query = text("SELECT id, username, hashed_password, is_admin FROM users WHERE email = :email OR username = :email")
    result = await db.execute(query, {"email": form_data.username})
    user = result.mappings().first()

    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username/email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": str(user["id"]), "is_admin": user["is_admin"]})
    return {"access_token": access_token, "token_type": "bearer"}

# -----------------------------------------------------------------------------
# TUS RESUMABLE MEDIA ENDPOINTS: /{user-id}/media
# -----------------------------------------------------------------------------

# 1. POST: Create Upload Session
@app.post("/api/v1/{user_id}/media", status_code=status.HTTP_201_CREATED)
async def create_tus_upload_session(
    user_id: str,
    request: Request,
    response: Response,
    upload_length: int = Header(..., alias="Upload-Length"),
    tus_resumable: str = Header(..., alias="Tus-Resumable"),
    upload_metadata: Optional[str] = Header(None, alias="Upload-Metadata"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    enforce_user_access(user_id, current_user)

    if tus_resumable != TUS_VERSION:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="Unsupported tus version")

    metadata = parse_tus_metadata(upload_metadata)
    raw_filename = metadata.get("filename", f"upload_{uuid.uuid4().hex}")
    extension = raw_filename.split(".")[-1] if "." in raw_filename else ""

    session_id = str(uuid.uuid4())
    temp_chunk_path = os.path.join(TEMP_DIR, f"{session_id}.part")
    
    # Initialize empty binary file
    with open(temp_chunk_path, "wb") as f:
        pass

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    insert_query = text("""
        INSERT INTO upload_sessions 
        (id, user_id, filename, total_size, current_offset, temp_chunk_path, status, metadata, expires_at)
        VALUES (:id, :user_id, :filename, :total_size, 0, :temp_chunk_path, 'in_progress', :metadata, :expires_at)
    """)
    
    await db.execute(insert_query, {
        "id": session_id,
        "user_id": user_id,
        "filename": raw_filename,
        "total_size": upload_length,
        "temp_chunk_path": temp_chunk_path,
        "metadata": str(metadata),
        "expires_at": expires_at
    })
    await db.commit()

    upload_url = f"{request.base_url}api/v1/{user_id}/media/{session_id}"
    
    response.headers["Tus-Resumable"] = TUS_VERSION
    response.headers["Location"] = upload_url
    response.headers["Upload-Expires"] = expires_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    return Response(status_code=status.HTTP_201_CREATED, headers=response.headers)

# 2. HEAD: Check Upload Offset
@app.head("/api/v1/{user_id}/media/{session_id}")
async def check_tus_upload_offset(
    user_id: str,
    session_id: str,
    response: Response,
    tus_resumable: str = Header(..., alias="Tus-Resumable"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    enforce_user_access(user_id, current_user)

    query = text("SELECT * FROM upload_sessions WHERE id = :id AND user_id = :user_id")
    result = await db.execute(query, {"id": session_id, "user_id": user_id})
    session = result.mappings().first()

    if not session or session["status"] != "in_progress":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found or completed")

    response.headers["Tus-Resumable"] = TUS_VERSION
    response.headers["Upload-Offset"] = str(session["current_offset"])
    response.headers["Upload-Length"] = str(session["total_size"])
    response.headers["Cache-Control"] = "no-store"
    
    return Response(status_code=status.HTTP_200_OK, headers=response.headers)

# 3. PATCH: Stream Chunk
@app.patch("/api/v1/{user_id}/media/{session_id}")
async def upload_tus_chunk(
    user_id: str,
    session_id: str,
    request: Request,
    response: Response,
    content_type: str = Header(..., alias="Content-Type"),
    upload_offset: int = Header(..., alias="Upload-Offset"),
    tus_resumable: str = Header(..., alias="Tus-Resumable"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    enforce_user_access(user_id, current_user)

    if content_type != "application/offset+octet-stream":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Content-Type must be application/offset+octet-stream")

    query = text("SELECT * FROM upload_sessions WHERE id = :id AND user_id = :user_id FOR UPDATE")
    result = await db.execute(query, {"id": session_id, "user_id": user_id})
    session = result.mappings().first()

    if not session or session["status"] != "in_progress":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active upload session not found")

    if upload_offset != session["current_offset"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Offset mismatch. Expected {session['current_offset']}, received {upload_offset}"
        )

    # Append streamed binary chunk directly to disk
    temp_path = session["temp_chunk_path"]
    written_bytes = 0

    with open(temp_path, "a+b") as f:
        f.seek(upload_offset)
        async for chunk in request.stream():
            f.write(chunk)
            written_bytes += len(chunk)

    new_offset = upload_offset + written_bytes

    if new_offset > session["total_size"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Exceeded total upload size")

    # Finalize upload if completed
    if new_offset == session["total_size"]:
        file_id = str(uuid.uuid4())
        raw_filename = session["filename"]
        extension = raw_filename.split(".")[-1] if "." in raw_filename else ""
        final_filename = f"{user_id}_{file_id}.{extension}" if extension else f"{user_id}_{file_id}"
        final_storage_path = os.path.join(FINAL_DIR, final_filename)

        shutil.move(temp_path, final_storage_path)

        # Record file item in files table
        insert_file = text("""
            INSERT INTO files (id, user_id, filename, extension, mime_type, size_bytes, storage_path)
            VALUES (:id, :user_id, :filename, :extension, :mime_type, :size_bytes, :storage_path)
        """)
        await db.execute(insert_file, {
            "id": file_id,
            "user_id": user_id,
            "filename": raw_filename,
            "extension": extension,
            "mime_type": "application/octet-stream",
            "size_bytes": session["total_size"],
            "storage_path": final_storage_path
        })

        # Update session status
        update_session = text("""
            UPDATE upload_sessions 
            SET current_offset = :offset, status = 'completed', file_id = :file_id, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """)
        await db.execute(update_session, {"offset": new_offset, "file_id": file_id, "id": session_id})
    else:
        # Session still in progress
        update_session = text("""
            UPDATE upload_sessions 
            SET current_offset = :offset, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """)
        await db.execute(update_session, {"offset": new_offset, "id": session_id})

    await db.commit()

    response.headers["Tus-Resumable"] = TUS_VERSION
    response.headers["Upload-Offset"] = str(new_offset)
    
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=response.headers)

# 4. DELETE: Cancel/Abort Upload Session
@app.delete("/api/v1/{user_id}/media/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_tus_upload_session(
    user_id: str,
    session_id: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    enforce_user_access(user_id, current_user)

    query = text("SELECT temp_chunk_path FROM upload_sessions WHERE id = :id AND user_id = :user_id")
    result = await db.execute(query, {"id": session_id, "user_id": user_id})
    session = result.mappings().first()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if os.path.exists(session["temp_chunk_path"]):
        os.remove(session["temp_chunk_path"])

    update_query = text("UPDATE upload_sessions SET status = 'aborted' WHERE id = :id")
    await db.execute(update_query, {"id": session_id})
    await db.commit()

    response.headers["Tus-Resumable"] = TUS_VERSION
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=response.headers)

# -----------------------------------------------------------------------------
# FILE READ & DELETE ENDPOINTS
# -----------------------------------------------------------------------------
@app.get("/api/v1/{user_id}/media")
async def list_user_media(
    user_id: str,
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    enforce_user_access(user_id, current_user)

    query = text("""
        SELECT id, filename, extension, mime_type, size_bytes, is_public, created_at
        FROM files
        WHERE user_id = :user_id
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """)
    result = await db.execute(query, {"user_id": user_id, "limit": limit, "offset": offset})
    return {"user_id": user_id, "files": result.mappings().all()}

@app.delete("/api/v1/{user_id}/media/files/{file_id}", status_code=status.HTTP_200_OK)
async def delete_completed_media(
    user_id: str,
    file_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    enforce_user_access(user_id, current_user)

    query = text("SELECT storage_path FROM files WHERE id = :file_id AND user_id = :user_id")
    result = await db.execute(query, {"file_id": file_id, "user_id": user_id})
    file = result.mappings().first()

    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if os.path.exists(file["storage_path"]):
        os.remove(file["storage_path"])

    delete_query = text("DELETE FROM files WHERE id = :file_id AND user_id = :user_id")
    await db.execute(delete_query, {"file_id": file_id, "user_id": user_id})
    await db.commit()

    return {"message": "File deleted successfully", "file_id": file_id}

# -----------------------------------------------------------------------------
# ADMIN ONLY ENDPOINTS
# -----------------------------------------------------------------------------
@app.get("/api/v1/admin/users")
async def admin_list_all_users(
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    query = text("SELECT id, username, email, is_admin, is_active, created_at FROM users")
    result = await db.execute(query)
    return {"users": result.mappings().all()}