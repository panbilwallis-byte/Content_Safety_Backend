from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.response import success

router = APIRouter()

UPLOAD_DIR = Path("storage/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {
    ".txt",
    ".csv",
    ".json",
    ".xlsx",
    ".pdf",
}

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024


@router.post("/files")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="文件名不能为空",
        )

    extension = Path(file.filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="只支持 TXT、CSV、JSON、XLSX、PDF 文件",
        )

    file_id = f"file_{uuid4().hex}"
    target_path = UPLOAD_DIR / f"{file_id}{extension}"

    total_size = 0

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

    return success(
        data={
            "file_id": file_id,
            "filename": file.filename,
            "size": total_size,
            "extension": extension,
        },
        message="文件上传成功",
    )