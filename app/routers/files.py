from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile, Request
from app.core.database import SessionLocal
import hashlib

from app.core.response import success

router = APIRouter()

UPLOAD_DIR = Path("storage/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {
    ".txt",
    ".csv",
    ".json",
    ".jsonl",
    ".xlsx",
    ".pdf",
}

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024


@router.post("/files")
async def upload_file(file: UploadFile = File(...), request: Request = None):
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="文件名不能为空",
        )
    if len(file.filename)>255:
        raise HTTPException(status_code=422,detail="文件名不能超过255字符")

    extension = Path(file.filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="只支持 TXT、CSV、JSON、JSONL、XLSX、PDF 文件",
        )

    file_id = f"file_{uuid4().hex}"
    target_path = UPLOAD_DIR / f"{file_id}{extension}"

    total_size = 0
    digest = hashlib.sha256()

    try:
        with target_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_FILE_SIZE:
                    target_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=400,
                        detail="文件大小不能超过 2GB",
                    )

                output.write(chunk)
                digest.update(chunk)

    except HTTPException:
        raise
    except Exception as error:
        target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="文件保存失败",
        ) from error
    finally:
        await file.close()

    if request is not None:
        from app.services.agent_trace_ingest import register_asset
        from fastapi.responses import JSONResponse
        from app.core.response import fail
        try:
            with SessionLocal() as db:
                from app.routers.agent_traces import actor
                data = register_asset(db, actor(request,db), file_id, file.filename, target_path,
                                      total_size, digest.hexdigest(), request.headers.get("X-Request-Id"))
                return success(data=data, message="文件上传成功")
        except HTTPException as exc:
            target_path.unlink(missing_ok=True)
            return JSONResponse(status_code=exc.status_code, content=fail(str(exc.detail), code=exc.status_code))
        except Exception:
            target_path.unlink(missing_ok=True)
            raise
    return success(
        data={
            "file_id": file_id,
            "filename": file.filename,
            "size": total_size,
            "extension": extension,
        },
        message="文件上传成功",
    )
