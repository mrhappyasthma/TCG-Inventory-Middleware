"""
Backup and restore: every database the deployment owns.

Both halves are administrative, and deliberately enforced on the endpoint
rather than by hiding the control. A download is the whole of somebody's
catalogue plus their listing settings, and a restore replaces a database
outright -- neither is something an ordinary account should reach even if it
guesses the URL.

The first router split out of `main`. The endpoints are unchanged from when
they lived there, including their paths, which is what let the existing tests
serve as the check that the move was faithful.
"""

import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

try:
    from app.deps import (
        DATABASE_URL,
        MAX_UPLOAD_BYTES,
        USER_DATABASE_URL,
        _owner_scope,
        db,
        inventory_for,
        inventory_path_for,
        require_admin_user,
        user_db,
    )
except ImportError:
    from .deps import (
        DATABASE_URL,
        MAX_UPLOAD_BYTES,
        USER_DATABASE_URL,
        _owner_scope,
        db,
        inventory_for,
        inventory_path_for,
        require_admin_user,
        user_db,
    )

router = APIRouter()


# Every SQLite file the deployment owns, so "download the database" can mean
# all of it rather than just the inventory. Each entry knows how to take its
# own consistent snapshot; a plain file copy is not safe while the app is
# running, because both databases are in WAL mode.
DATABASE_FILES = {
    "inventory": {
        "label": "Inventory",
        "stem": "tcg-inventory",
        "description": (
            "Card catalogue, eBay links, catalogued quantities, per-user "
            "pricing rules and listing settings, and cover photo overrides."
        ),
        "path": lambda: DATABASE_URL,
        "export": lambda dest: db.export_snapshot(dest),
        "summary": lambda: {
            "cards": db.get_stats()["total_cards"],
            "linked_to_ebay": db.get_stats()["active_listings"],
        },
    },
    "users": {
        "label": "Users",
        "stem": "tcg-users",
        "description": (
            "Google accounts, roles and approval status. Contains no passwords "
            "and no OAuth secrets -- sign-in is delegated to Google."
        ),
        "path": lambda: USER_DATABASE_URL,
        "export": lambda dest: user_db.export_snapshot(dest),
        "summary": lambda: {"accounts": user_db.count_users()},
    },
}


def database_specs() -> Dict[str, Dict[str, Any]]:
    """
    Every database the deployment owns, including one per extra account.

    `DATABASE_FILES` describes the two fixed files. Since each account now
    keeps its own inventory, the rest are discovered from the user list --
    and only the ones that exist on disk are offered, because an account that
    has never signed in has no file yet and a zero-byte member in a backup is
    worse than an absent one.

    Computed per call rather than cached: a backup taken after a new account
    joined has to include them, and nobody is going to restart the container
    to make that true.
    """
    specs = dict(DATABASE_FILES)
    owner = _owner_scope()
    for account in user_db.list_all_users():
        user_id = int(account["id"])
        if user_id == owner:
            continue
        path = inventory_path_for(user_id)
        if not os.path.exists(path):
            continue
        label = account.get("username") or account.get("email") or f"user {user_id}"
        specs[f"inventory-user-{user_id}"] = {
            "label": f"Inventory ({label})",
            "stem": f"tcg-inventory-user-{user_id}",
            "description": (
                f"Card catalogue, eBay links, quantities, pricing rules and "
                f"listing settings belonging to {label}."
            ),
            "path": (lambda p=path: p),
            "export": (
                lambda dest, uid=user_id: inventory_for(uid).export_snapshot(dest)
            ),
            "summary": (
                lambda uid=user_id: {
                    "cards": inventory_for(uid).get_stats()["total_cards"],
                }
            ),
        }
    return specs


@router.get("/api/database/files")
def list_database_files(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    What is available to download, so the UI does not hardcode the list.

    Admin only, matching the downloads themselves.
    """
    entries = []
    for name, spec in database_specs().items():
        path = os.path.abspath(spec["path"]())
        try:
            summary = spec["summary"]()
        except Exception:
            # A summary is cosmetic; never let it block a backup.
            summary = {}
        entries.append(
            {
                "name": name,
                "label": spec["label"],
                "description": spec["description"],
                "filename": os.path.basename(path),
                "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                "summary": summary,
            }
        )
    return {"files": entries}


@router.get("/api/database/download/{name}")
def download_database_file(
    name: str,
    background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Download one database as a consistent snapshot. Admin only."""
    spec = database_specs().get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No such database: {name}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="tcg-snapshot-")
    dest = os.path.join(tmp_dir, f"{spec['stem']}-{stamp}.db")

    try:
        spec["export"](dest)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Could not create a snapshot: {exc}"
        )

    background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        dest,
        media_type="application/vnd.sqlite3",
        filename=os.path.basename(dest),
        background=background,
    )


@router.get("/api/database/bundle")
def download_database_bundle(
    background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Download every database in one zip. Admin only.

    Each member is a VACUUM INTO snapshot rather than a copied file, so the
    archive is internally consistent and restorable without sidecars. A short
    README is included because a bare pair of .db files is not self-describing
    six months later.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="tcg-bundle-")
    archive = os.path.join(tmp_dir, f"tcg-databases-{stamp}.zip")
    notes = [
        f"TCG Inventory Middleware -- database backup taken {stamp}",
        "",
        "Each .db is a VACUUM INTO snapshot: complete on its own, with no",
        "-wal or -shm sidecar needed.",
        "",
    ]

    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, spec in database_specs().items():
                member = f"{spec['stem']}-{stamp}.db"
                staged = os.path.join(tmp_dir, member)
                spec["export"](staged)
                bundle.write(staged, arcname=member)
                os.remove(staged)
                notes.append(f"{member}")
                notes.append(f"    {spec['description']}")
                notes.append("")
            bundle.writestr("README.txt", chr(10).join(notes))
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Could not build the backup archive: {exc}"
        )

    background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=os.path.basename(archive),
        background=background,
    )


@router.get("/api/inventory/database")
def download_inventory_database(
    background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Download a consistent snapshot of the inventory database. Admin only.

    Both halves of backup/restore are administrative: the file is the whole
    shared catalog plus every listing setting, which is more than an ordinary
    user needs in order to work. The endpoint is restricted rather than merely
    hidden, so the permission does not depend on the UI.

    Taken with VACUUM INTO so the write-ahead log is checkpointed into the file.
    A hand-copied .db can otherwise be missing its most recent commits.
    """
    inv = inventory_for(admin)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="tcg-snapshot-")
    dest = os.path.join(tmp_dir, f"tcg-inventory-{stamp}.db")

    try:
        inv.export_snapshot(dest)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"Could not create a snapshot: {exc}"
        )

    # Remove the temp copy once the response has been sent.
    background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)

    return FileResponse(
        dest,
        media_type="application/vnd.sqlite3",
        filename=os.path.basename(dest),
        background=background,
    )


@router.post("/api/inventory/database")
async def import_inventory_database(
    file: UploadFile = File(...),
    confirm: bool = Form(False),
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Replace the inventory database with an uploaded snapshot. Admin only.

    Restricted to admins because the catalog is shared: a restore replaces
    everyone's data, not just the uploader's.

    Without confirm=true the upload is only validated and summarised, so an
    operator can see what a restore would bring in before committing to it. The
    current database is copied aside first either way, so a restore is
    reversible.
    """
    inv = inventory_for(admin)
    tmp_dir = tempfile.mkdtemp(prefix="tcg-import-")
    staged = os.path.join(tmp_dir, "upload.db")
    try:
        written = 0
        with open(staged, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"That database is larger than the "
                            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload "
                            f"limit. Raise MAX_UPLOAD_MB to restore it."
                        ),
                    )
                out.write(chunk)

        check = inv.inspect_snapshot(staged)
        if not check["ok"]:
            raise HTTPException(status_code=400, detail=check["error"])

        current = inv.get_stats()

        if not confirm:
            return {
                "applied": False,
                "filename": file.filename,
                "incoming": check["counts"],
                "current": {
                    "manifest": current["total_cards"],
                    "ebay_variations": current["active_listings"],
                },
                "message": (
                    "Validated but not applied. Re-send with confirm=true to "
                    "replace the current inventory database."
                ),
            }

        result = inv.replace_with_snapshot(staged)
        return {
            "applied": True,
            "filename": file.filename,
            "incoming": result["counts"],
            "backup_path": result["backup_path"],
            "message": "Inventory database replaced. A backup of the previous one was kept.",
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


