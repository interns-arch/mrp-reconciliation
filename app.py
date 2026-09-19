"""
MRP Reconciliation - local, fully offline web app.

Run:  python app.py        then open http://127.0.0.1:8765

Everything (upload, parsing, matching, CSV generation) happens in this process,
against files on this machine. The app makes no outbound network calls and the
frontend loads nothing but its own /static assets.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import hmac
import io
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser

import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import mrp_core as core
from mrp_core import UserError

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# One upload folder per process. A shared folder meant that stopping any other
# instance of this app deleted the files belonging to the running one, and every
# upload after that died with FileNotFoundError.
UPLOAD_ROOT = os.path.join(tempfile.gettempdir(), "mrp_recon_uploads")
UPLOAD_DIR = os.path.join(UPLOAD_ROOT, f"run_{os.getpid()}")


def ensure_upload_dir() -> str:
    """Make sure the upload folder exists. Cheap, and called before every save:
    temp cleaners and other instances can remove it while we are running."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    return UPLOAD_DIR


def prune_old_upload_dirs(max_age_days: int = 7) -> None:
    """Drop folders left behind by instances that were killed, never ours."""
    cutoff = time.time() - max_age_days * 86400
    try:
        entries = os.listdir(UPLOAD_ROOT)
    except OSError:
        return
    for name in entries:
        path = os.path.join(UPLOAD_ROOT, name)
        if path == UPLOAD_DIR or not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


ensure_upload_dir()

ALLOWED_EXT = {".csv", ".txt", ".tsv", ".xlsx", ".xlsm", ".xls"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Running on a server rather than on the user's own machine changes two things
# that are not safe to leave as they are locally.
IS_HOSTED = bool(os.environ.get("RENDER") or os.environ.get("MRP_HOSTED"))

# 1. "Point at a file on disk" reads the machine the app runs on. Locally that
#    is the user's own PC and it is the whole point; on a server it would let
#    any visitor read files off the host, so it is off unless switched on.
ALLOW_LOCAL_PATHS = _env_flag("MRP_ALLOW_LOCAL_PATHS", not IS_HOSTED)

# 2. There is no login locally because only the user can reach 127.0.0.1. On a
#    public URL the inventory and supplier price lists would be open to anyone,
#    so a password is required.
AUTH_USER = os.environ.get("MRP_USER", "admin")
AUTH_PASSWORD = os.environ.get("MRP_PASSWORD", "")

# An explicit, separate opt-out. This exists so "no password" on a public URL
# is always something someone chose on purpose, in one place they had to type
# out, rather than something that happens by leaving MRP_PASSWORD blank.
REQUIRE_AUTH = _env_flag("MRP_REQUIRE_AUTH", True)

# Checked at import, not in main(): a hosted deployment starts the app through
# gunicorn, which never calls main(), so a check there would never run.
if IS_HOSTED and REQUIRE_AUTH and not AUTH_PASSWORD:
    raise RuntimeError(
        "MRP Reconciliation refuses to start: MRP_HOSTED is set but MRP_PASSWORD is "
        "empty. Without a password this URL would expose your inventory and supplier "
        "price lists to anyone who finds it. Set MRP_PASSWORD (and optionally MRP_USER), "
        "or set MRP_REQUIRE_AUTH=0 to run with no login at all (not recommended for a "
        "public URL), then deploy again."
    )

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB uploads
app.config["JSON_SORT_KEYS"] = False

# Changes on every start, so the page can tell that the server it uploaded to
# is gone and that its file selections are stale.
SERVER_ID = uuid.uuid4().hex[:12]

# In-process state. Single-user local tool, so a plain dict + lock is enough.
_LOCK = threading.Lock()
FILES: dict[str, dict] = {}     # file_id -> {path, name, kind, size}
RUNS: dict[str, dict] = {}      # run_id  -> {step1: df, step2: df, ...}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.errorhandler(413)
def too_large(_e):
    return fail("That file is larger than the 1 GB upload limit.", 413)


@app.errorhandler(Exception)
def unhandled(e):
    if isinstance(e, UserError):
        return fail(str(e))
    app.logger.error("Unhandled error:\n%s", traceback.format_exc())
    return fail(f"Unexpected server error: {type(e).__name__}: {e}", 500)


def register_file(path: str, name: str, kind: str) -> str:
    file_id = uuid.uuid4().hex[:12]
    with _LOCK:
        FILES[file_id] = {"path": path, "name": name, "kind": kind,
                          "size": os.path.getsize(path)}
    return file_id


def stash_upload(storage, kind: str) -> str:
    """Save an uploaded file to the temp dir and return its file_id."""
    original = storage.filename or "upload"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_EXT:
        raise UserError(
            f"'{original}' has an unsupported extension '{ext or '(none)'}'. "
            f"Upload a CSV or Excel file ({', '.join(sorted(ALLOWED_EXT))})."
        )
    safe = secure_filename(original) or "upload"
    dest = os.path.join(ensure_upload_dir(), f"{uuid.uuid4().hex[:12]}_{safe}")
    try:
        storage.save(dest)
    except OSError as exc:
        raise UserError(
            f"Could not save '{original}' to the temporary upload folder "
            f"({UPLOAD_DIR}): {exc}. Free some disk space or restart the app, then "
            f"re-select your files. You can also use 'or point at a file on disk' "
            f"instead, which reads the file where it is and copies nothing."
        ) from exc
    return register_file(dest, original, kind)


def adopt_path(raw_path: str, kind: str) -> str:
    """Register a file the user pointed at on disk instead of uploading."""
    if not ALLOW_LOCAL_PATHS:
        raise UserError(
            "Reading files by path is disabled on this server - that option reads the "
            "machine the app runs on, not yours. Use the file chooser to upload instead."
        )
    p = os.path.expanduser(os.path.expandvars(raw_path.strip().strip('"').strip("'")))
    if not p:
        raise UserError("Enter a file path.")
    if not os.path.isfile(p):
        raise UserError(f"No file found at: {p}")
    ext = os.path.splitext(p)[1].lower()
    if ext not in ALLOWED_EXT:
        raise UserError(
            f"'{os.path.basename(p)}' has an unsupported extension '{ext or '(none)'}'. "
            f"Use a CSV or Excel file."
        )
    return register_file(p, os.path.basename(p), kind)


def load_inventory_cached(entry: dict):
    """Parse the inventory once; /api/run then reuses what the upload already read."""
    key = ("inventory", entry["path"], os.path.getmtime(entry["path"]))
    if entry.get("_key") != key:
        entry["_key"] = key
        entry["_loaded"] = core.load_inventory(entry["path"])
    return entry["_loaded"]


def load_stock_cached(entry: dict):
    key = ("stock", entry["path"], os.path.getmtime(entry["path"]))
    if entry.get("_key") != key:
        entry["_key"] = key
        entry["_loaded"] = core.load_stock(entry["path"])
    return entry["_loaded"]


def load_mrp_cached(entry: dict, **kwargs):
    """Cache per column-mapping: changing a dropdown re-parses, re-running doesn't."""
    key = ("mrp", os.path.getmtime(entry["path"]), tuple(sorted(
        (k, str(v)) for k, v in kwargs.items())))
    if entry.get("_key") != key:
        entry["_key"] = key
        entry["_loaded"] = core.load_mrp_list(entry["path"], **kwargs)
    return entry["_loaded"]


def get_file(file_id: str) -> dict:
    with _LOCK:
        entry = FILES.get(file_id)
    if not entry:
        raise UserError(
            "That file is no longer registered with the server - this happens when the "
            "app is restarted. Re-select your files on this page and run the check again."
        )
    if not os.path.isfile(entry["path"]):
        uploaded = entry["path"].startswith(UPLOAD_ROOT)
        raise UserError(
            f"'{entry['name']}' is no longer readable at {entry['path']}. "
            + ("The temporary copy was removed (a disk cleaner, or another copy of this "
               "app shutting down). Re-select the file and run again."
               if uploaded else
               "The file was moved, renamed or deleted since you pointed at it.")
        )
    return entry


def take_input(kind: str):
    """Accept either an uploaded file or a 'path' form field. Returns file_id or None."""
    storage = request.files.get("file")
    if storage is not None and storage.filename:
        return stash_upload(storage, kind)
    raw = (request.form.get("path") or "").strip()
    if raw:
        return adopt_path(raw, kind)
    return None


def df_page(df: pd.DataFrame, columns: list, args) -> dict:
    """Filter / sort / paginate a result frame for the browser."""
    view = df
    brand = (args.get("brand") or "").strip()
    issue = (args.get("issue_type") or "").strip()
    query = (args.get("q") or "").strip().upper()
    if brand:
        view = view[view["brand"] == brand]
    if issue and "issue_type" in view.columns:   # the per-brand lists have no issue_type
        view = view[view["issue_type"] == issue]
    if query:
        mask = (view["part_no"].astype(str).str.upper().str.contains(query, regex=False)
                | view["part_name"].astype(str).str.upper().str.contains(query, regex=False))
        view = view[mask]

    sort = args.get("sort") or ""
    if sort in columns:
        view = view.sort_values(sort, ascending=(args.get("dir", "asc") != "desc"),
                                kind="stable", na_position="last")

    total = len(view)
    try:
        page_size = max(10, min(500, int(args.get("page_size", 50))))
    except ValueError:
        page_size = 50
    pages = max(1, (total + page_size - 1) // page_size)
    try:
        page = max(1, min(pages, int(args.get("page", 1))))
    except ValueError:
        page = 1
    chunk = view.iloc[(page - 1) * page_size: page * page_size]

    rows = []
    for rec in chunk[columns].to_dict(orient="records"):
        row = {}
        for k, v in rec.items():
            if v is None or (isinstance(v, float) and pd.isna(v)):
                row[k] = ""
            elif isinstance(v, float):
                if k == "mrp_change_pct":
                    row[k] = f"{v:+.1f}%"
                elif k == "mrp_difference":
                    row[k] = f"{v:+,.2f}"
                elif k.startswith("mrp"):
                    row[k] = f"{v:,.2f}"
                else:
                    row[k] = f"{v:g}"
            else:
                row[k] = str(v)
        rows.append(row)
    return {"rows": rows, "total": total, "page": page, "pages": pages,
            "page_size": page_size, "columns": columns}


def get_run(run_id: str) -> dict:
    with _LOCK:
        run = RUNS.get(run_id)
    if not run:
        raise UserError("That result set has expired - press 'Run check' again.")
    return run


# ---------------------------------------------------------------------------
# Static shell
# ---------------------------------------------------------------------------

def _auth_ok(header: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    # compare_digest on both halves so neither can be guessed a byte at a time
    return (hmac.compare_digest(user, AUTH_USER)
            and hmac.compare_digest(password, AUTH_PASSWORD))


@app.before_request
def require_password():
    """Only active when MRP_PASSWORD is set - a local run stays password-free."""
    if not AUTH_PASSWORD:
        return None
    if _auth_ok(request.headers.get("Authorization", "")):
        return None
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="MRP Reconciliation", charset="UTF-8"'},
    )


@app.after_request
def no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-MRP-Server"] = SERVER_ID
    # Belt and braces: the page may not reach out even if someone edits the JS.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "form-action 'self'; base-uri 'none'"
    )
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/mode")
def api_mode():
    """Lets the page describe itself honestly: offline-local vs hosted."""
    return jsonify({
        "ok": True,
        "hosted": IS_HOSTED,
        "allow_local_paths": ALLOW_LOCAL_PATHS,
        "auth": bool(AUTH_PASSWORD),
    })


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


# ---------------------------------------------------------------------------
# Upload endpoints
# ---------------------------------------------------------------------------

@app.post("/api/inventory")
def api_inventory():
    file_id = take_input("inventory")
    if not file_id:
        return fail("Choose an inventory CSV, or paste the path to one.")
    entry = get_file(file_id)
    result = load_inventory_cached(entry)
    with _LOCK:
        FILES[file_id]["brands"] = result.stats["brands"]
    return jsonify({
        "ok": True,
        "file_id": file_id,
        "name": entry["name"],
        "size": entry["size"],
        "rows_read": result.rows_read,
        "unique_parts": int(result.stats["unique_parts"]),
        "brands": result.stats["brands"],
        "warnings": result.warnings,
    })


@app.post("/api/stock")
def api_stock():
    file_id = take_input("stock")
    if not file_id:
        return fail("Choose a stock CSV, or paste the path to one.")
    entry = get_file(file_id)
    result = load_stock_cached(entry)
    return jsonify({
        "ok": True,
        "file_id": file_id,
        "name": entry["name"],
        "size": entry["size"],
        "rows_read": result.rows_read,
        "available": bool(result.stats.get("available")),
        "has_brand": bool(result.stats.get("has_brand")),
        "unique_parts": int(result.stats.get("unique_parts", 0)),
        "warnings": result.warnings,
    })


@app.post("/api/mrp")
def api_mrp():
    """Stash an MRP file and return its headers so the UI can build the dropdowns."""
    file_id = take_input("mrp")
    if not file_id:
        return fail("Choose an MRP list (CSV or XLSX), or paste the path to one.")
    entry = get_file(file_id)
    info = core.sniff_file(entry["path"])
    info.update({"ok": True, "file_id": file_id, "name": entry["name"],
                 "size": entry["size"]})
    return jsonify(info)


@app.post("/api/mrp/resniff")
def api_mrp_resniff():
    """Re-read the headers after the user changes the sheet or header row."""
    body = request.get_json(force=True, silent=True) or {}
    entry = get_file(body.get("file_id", ""))
    info = core.sniff_file(entry["path"], sheet=body.get("sheet") or 0,
                           header_row=body.get("header_row"))
    info.update({"ok": True, "file_id": body.get("file_id"), "name": entry["name"]})
    return jsonify(info)


# ---------------------------------------------------------------------------
# The actual check
# ---------------------------------------------------------------------------

@app.post("/api/run")
def api_run():
    body = request.get_json(force=True, silent=True) or {}
    inv_id = body.get("inventory_file_id")
    if not inv_id:
        return fail("Upload the inventory master first.")
    mrp_specs = body.get("mrp_lists") or []
    if not mrp_specs:
        return fail("Add at least one brand MRP list before running the check.")

    warnings: list[str] = []

    inv_entry = get_file(inv_id)
    inv_res = load_inventory_cached(inv_entry)
    warnings.extend(inv_res.warnings)

    stock_id = body.get("stock_file_id")
    stock_res = (load_stock_cached(get_file(stock_id)) if stock_id
                 else core.load_stock(None))
    warnings.extend(stock_res.warnings)
    stock_available = bool(stock_res.stats.get("available"))

    mrp_frames = []
    per_file = []
    suspect_mapping = []
    for spec in mrp_specs:
        entry = get_file(spec.get("file_id", ""))
        brand = (spec.get("brand") or "").strip()
        if not brand:
            return fail(f"Pick the brand for MRP file '{entry['name']}'.")
        part_col = spec.get("part_col")
        mrp_col = spec.get("mrp_col")
        if not part_col or not mrp_col:
            return fail(
                f"MRP file '{entry['name']}': choose both the part-number column and the "
                f"MRP column."
            )
        sheet = spec.get("sheet") or 0
        header_row = spec.get("header_row") or 0

        # Confirm the mapping still belongs to THIS file/sheet before parsing it.
        # Without this a stale selection (a file swapped in the same block, a
        # sheet changed after the columns were picked) is read silently against
        # the wrong sheet and every part comes out as "new".
        if part_col == mrp_col:
            return fail(
                f"The MRP list you marked as {brand} has '{part_col}' selected as BOTH the "
                f"part-number column and the MRP column. One column cannot be both. Pick the "
                f"column that holds the part numbers, then run the check again."
            )

        actual = core.sniff_file(entry["path"], sheet=sheet, header_row=header_row)
        unknown = [c for c in (part_col, spec.get("desc_col"), mrp_col)
                   if c and c not in actual["columns"]]
        if unknown:
            where = f"'{entry['name']}'"
            if isinstance(sheet, str):
                where += f", sheet '{sheet}'"
            return fail(
                f"The MRP list you marked as {brand} does not line up with the file it "
                f"points at. Column(s) {', '.join(repr(c) for c in unknown)} are not in "
                f"{where} on header row {header_row + 1} - that sheet actually has "
                f"{', '.join(actual['columns'])}. "
                f"Re-select the file for this brand (or fix the sheet / header row / "
                f"column dropdowns) and run the check again."
            )

        res = load_mrp_cached(
            entry, brand=brand, part_col=part_col,
            desc_col=spec.get("desc_col") or None, mrp_col=mrp_col,
            sheet=sheet, header_row=header_row, display_name=entry["name"],
        )
        mrp_frames.append(res.df)
        warnings.extend(res.warnings)
        # A part-number column should be almost entirely unique and non-blank.
        # Heavy duplication or blankness means the wrong column was picked.
        d = res.stats["dropped"]
        considered = res.rows_read - d["header_repeat"] - d["all_blank_or_zero"]
        if considered >= 20:
            dup_rate = d["duplicates"] / considered
            blank_rate = d["no_part_no"] / considered
            if dup_rate > 0.2 or blank_rate > 0.2:
                suspect_mapping.append(
                    f"{brand} ('{entry['name']}'): column '{part_col}' does not look like a "
                    f"part-number column - of {considered:,} rows, {d['duplicates']:,} "
                    f"({dup_rate:.0%}) are duplicates and {d['no_part_no']:,} "
                    f"({blank_rate:.0%}) are blank. A part number should be unique on "
                    f"almost every row. Check the 'Part number column' dropdown."
                )

        per_file.append({
            "name": entry["name"], "brand": brand,
            "rows_read": res.rows_read,
            "usable_rows": int(res.stats["usable_rows"]),
            "dropped": res.stats["dropped"],
        })

    step1, s1_summary, classified = core.run_step1(inv_res.df, mrp_frames)
    warnings.extend(s1_summary["warnings"])

    step2, s2_stats = core.run_step2(
        step1, stock_res.df, stock_available,
        bool(stock_res.stats.get("has_brand")),
    )

    lists = core.split_lists(classified, stock_res.df, stock_available,
                             bool(stock_res.stats.get("has_brand")))

    # Loud alarm for the classic mistake: the right brand name on the wrong
    # file, or the wrong part-number column - the whole list then reads as new.
    mismatched_brands = []
    for brand, s in s1_summary["per_brand"].items():
        listed = s["mrp_list_parts"]
        if listed >= 5 and s["matched"] == 0:
            mismatched_brands.append(
                f"{brand}: not one of the {listed:,} part numbers on this list was found "
                f"under brand '{brand}' in Inventory."
            )
        elif listed >= 50 and s["matched"] / listed < 0.02:
            mismatched_brands.append(
                f"{brand}: only {s['matched']:,} of {listed:,} part numbers on this list "
                f"({s['matched'] / listed:.1%}) were found under brand '{brand}' in Inventory."
            )

    run_id = uuid.uuid4().hex[:12]
    with _LOCK:
        RUNS.clear()  # single-user tool: only the latest run is kept in memory
        RUNS[run_id] = {"step1": step1, "step2": step2, **lists}

    def counts(df):
        if df.empty:
            return {}
        grouped = df.groupby(["brand", "issue_type"]).size()
        out: dict[str, dict] = {}
        for (brand, issue), n in grouped.items():
            out.setdefault(str(brand), {})[str(issue)] = int(n)
        return out

    def totals(df):
        if df.empty:
            return {t: 0 for t in core.REPORTED_ISSUE_TYPES}
        vc = df["issue_type"].value_counts()
        return {t: int(vc.get(t, 0)) for t in core.REPORTED_ISSUE_TYPES}

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "stock_data_available": stock_available,
        "stock_warning": None if stock_available else (
            "No stock data was available - all hikes were assumed safe to ignore. "
            "Re-run once a real stock export is available."
        ),
        "inventory": {
            "name": inv_entry["name"], "rows_read": inv_res.rows_read,
            "unique_parts": int(inv_res.stats["unique_parts"]),
        },
        "stock": {
            "rows_read": stock_res.rows_read,
            "unique_parts": int(stock_res.stats.get("unique_parts", 0)),
        },
        "lists": {
            key: {
                "total": int(len(lists[key])),
                "label": spec["label"],
                "blurb": spec["blurb"],
                "file": spec["file"],
                "by_brand": {str(b): int(n) for b, n
                             in lists[key]["brand"].value_counts().items()}
                if not lists[key].empty else {},
            }
            for key, spec in core.LIST_SPECS.items()
        },
        "list_order": core.LIST_KEYS,
        "no_mrp_on_list": int(len(lists["zero"])),
        "brand_mismatch": mismatched_brands,
        "suspect_mapping": suspect_mapping,
        "mrp_files": per_file,
        "per_brand": s1_summary["per_brand"],
        "matched_ok": s1_summary["matched_ok"],
        "skipped_brands": s1_summary["skipped_brands"],
        "step1": {"total": int(len(step1)), "by_brand": counts(step1), "totals": totals(step1)},
        "step2": {"total": int(len(step2)), "by_brand": counts(step2), "totals": totals(step2),
                  "dropped": s2_stats["dropped"],
                  "dropped_reason": s2_stats.get("dropped_reason", "")},
        "brands": sorted({str(b) for b in step1["brand"].unique()}) if not step1.empty else [],
        "issue_types": core.REPORTED_ISSUE_TYPES,
        "warnings": warnings,
    })


# ---------------------------------------------------------------------------
# Results: paginated table data + CSV download
# ---------------------------------------------------------------------------

RESULT_SETS = {
    "step1": ("step1_inventory_vs_mrp_issues.csv", core.STEP1_COLUMNS),
    "step2": ("step2_final_issues_after_stock_check.csv", core.STEP2_COLUMNS),
    **{k: (v["file"], v["columns"]) for k, v in core.LIST_SPECS.items()},
}


@app.get("/api/results/<run_id>/<step>")
def api_results(run_id: str, step: str):
    if step not in RESULT_SETS:
        return fail(f"Unknown result set '{step}'.")
    run = get_run(run_id)
    return jsonify({"ok": True, **df_page(run[step], RESULT_SETS[step][1], request.args)})


@app.get("/api/download/<run_id>/<step>")
def api_download(run_id: str, step: str):
    if step not in RESULT_SETS:
        return fail(f"Unknown result set '{step}'.")
    run = get_run(run_id)
    filename, columns = RESULT_SETS[step]
    df = run[step]

    # Honour the on-screen filters when the user asks for the filtered view.
    if request.args.get("filtered") == "1":
        brand = (request.args.get("brand") or "").strip()
        issue = (request.args.get("issue_type") or "").strip()
        if brand:
            df = df[df["brand"] == brand]
        if issue and "issue_type" in df.columns:
            df = df[df["issue_type"] == issue]

    out = df[columns].copy()
    for col in ("mrp_inventory", "mrp_official", "mrp", "mrp_old", "mrp_new",
                "mrp_difference", "mrp_change_pct"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{float(v):.2f}")
    if "available_quantity" in out.columns:
        out["available_quantity"] = out["available_quantity"].map(
            lambda v: "" if pd.isna(v) else f"{float(v):g}")

    buf = io.StringIO()
    out.to_csv(buf, index=False, lineterminator="\n")
    data = buf.getvalue().encode("utf-8-sig")
    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(data)),
    })


@app.post("/api/reset")
def api_reset():
    with _LOCK:
        RUNS.clear()
        stale = [fid for fid, e in FILES.items() if e["path"].startswith(UPLOAD_DIR)]
        for fid in stale:
            try:
                os.remove(FILES[fid]["path"])
            except OSError:
                pass
            FILES.pop(fid, None)
        FILES.clear()
    return jsonify({"ok": True})


@atexit.register
def _cleanup():
    # Only this process's own folder. Removing the shared root here used to
    # delete the uploads of any other instance still running.
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    try:
        os.rmdir(UPLOAD_ROOT)      # succeeds only when nothing else is left
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Local MRP reconciliation app (offline).")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--host", default=os.environ.get("HOST",
                                                     "0.0.0.0" if IS_HOSTED else "127.0.0.1"))
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    prune_old_upload_dirs()

    if IS_HOSTED and not AUTH_PASSWORD:
        raise SystemExit(
            "Refusing to start: this looks like a hosted deployment but MRP_PASSWORD is "
            "not set, which would leave your inventory and price lists open to anyone "
            "with the URL. Set MRP_PASSWORD (and optionally MRP_USER) and redeploy."
        )

    url = f"http://{args.host}:{args.port}/"
    print("=" * 68)
    print("  MRP Reconciliation - running locally, fully offline")
    print(f"  Open: {url}")
    print("  Press Ctrl+C to stop.")
    print("=" * 68)
    if not args.no_browser and not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
