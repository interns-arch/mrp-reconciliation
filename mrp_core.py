"""
Core data logic for the MRP reconciliation app.

Pure pandas / stdlib. No network access of any kind is performed here, and none
is possible: the only I/O is reading local files the user chose.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INVENTORY_REQUIRED = ["part_no", "brand", "part_name", "mrp"]
STOCK_REQUIRED = ["part_no", "available_quantity"]

ISSUE_MRP_MISMATCH = "MRP_MISMATCH"
ISSUE_MRP_ZERO = "MRP_LIST_SHOWS_ZERO"
ISSUE_MISSING_IN_MRP = "MISSING_IN_MRP_LIST"
ISSUE_MISSING_IN_INV = "MISSING_IN_INVENTORY"

ISSUE_TYPES = [ISSUE_MRP_MISMATCH, ISSUE_MRP_ZERO, ISSUE_MISSING_IN_MRP, ISSUE_MISSING_IN_INV]

# What the Step 1 / Step 2 issue reports actually contain. A part whose official
# MRP is 0 or blank is a bulk/non-retail pack with no printed MRP - it recurs on
# every run and cannot be mispriced against an MRP it does not have, so it is
# kept out of the issue reports and lives in its own list instead.
REPORTED_ISSUE_TYPES = [ISSUE_MRP_MISMATCH, ISSUE_MISSING_IN_MRP, ISSUE_MISSING_IN_INV]
NOT_AN_ISSUE = (ISSUE_MRP_ZERO,)

# Not an issue, so it never reaches the Step 1 report - but it is one of the
# per-brand lists, so the classification keeps it.
MATCHED_SAME_MRP = "MATCHED_SAME_MRP"

# The per-brand lists: for one uploaded MRP list, every usable row on it lands
# in exactly one of these four files.
LIST_SPECS = {
    "same": {
        "file": "list1_matched_same_mrp.csv",
        "label": "Same MRP",
        "blurb": "On the MRP list and in Inventory with an identical MRP - nothing to do.",
        "columns": ["brand", "part_no", "part_name", "mrp"],
    },
    "changed": {
        "file": "list2_mrp_changed_old_vs_new.csv",
        "label": "MRP changed",
        "blurb": "In Inventory but the supplier's MRP differs - old and new price side by side.",
        "columns": ["brand", "part_no", "part_name", "mrp_old", "mrp_new",
                    "mrp_direction", "mrp_difference", "mrp_change_pct"],
    },
    "new": {
        "file": "list3_new_parts_not_in_inventory.csv",
        "label": "New parts",
        "blurb": "On the MRP list but not set up in Inventory - these are new parts.",
        "columns": ["brand", "part_no", "part_name", "mrp_new", "status"],
    },
    "zero": {
        "file": "list4_mrp_list_shows_zero.csv",
        "label": "No MRP on list",
        "blurb": "In Inventory, but the MRP list carries 0 or a blank - typically a bulk "
                 "or non-retail pack (drums, barrels) that has no printed MRP. Not a price "
                 "issue, so these are kept out of Step 1 and Step 2 and listed only here.",
        "columns": ["brand", "part_no", "part_name", "mrp_inventory", "note"],
    },
}
LIST_KEYS = list(LIST_SPECS)

STEP1_COLUMNS = [
    "brand",
    "part_no",
    "part_name",
    "issue_type",
    "mrp_inventory",
    "mrp_official",
    "mrp_direction",
    "note",
]
STEP2_COLUMNS = STEP1_COLUMNS + [
    "available_quantity",
    "in_stock",
    "stock_data_available",
    "decision_reason",
]

# Money comparison tolerance (half a paisa) so 450.0 == 450.000001 never fires.
MRP_TOLERANCE = 0.005

ZERO_NOTE = (
    "Official MRP list shows 0 for this part - likely a bulk/non-retail pack "
    "with no printed MRP; verify manually."
)
BLANK_NOTE = (
    "Official MRP list has no MRP value for this part (cell was blank) - "
    "likely a bulk/non-retail pack with no printed MRP; verify manually."
)


class UserError(Exception):
    """An error that is the user's to fix; shown verbatim in the UI."""


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_MONEY_STRIP = re.compile(r"[^\d.\-]")
_WS = re.compile(r"\s+")


def norm_key(value: Any) -> str:
    """Normalise a part number / brand for matching: trim + uppercase."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            value = int(value)
    s = str(value).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    # Excel and sloppy CSV exports turn 10012345 into "10012345.0".
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return _WS.sub(" ", s).upper()


def norm_series(s: pd.Series) -> pd.Series:
    """Vectorised norm_key for a whole column.

    The two regex passes cost ~0.6s each per 750k rows and almost never match,
    so each one is gated behind a plain substring probe that costs ~0.15s.
    """
    out = s.astype("string").fillna("").str.strip().str.upper()
    if out.str.contains("  ", regex=False).any() or out.str.contains("\t", regex=False).any():
        out = out.str.replace(r"\s+", " ", regex=True)
    if out.str.contains(".0", regex=False).any():
        # Excel and sloppy exports turn part number 10012345 into "10012345.0".
        out = out.str.replace(r"^(-?\d+)\.0+$", r"\1", regex=True)
    out = out.where(~out.isin(["NAN", "NONE", "NAT"]), "")
    return out.astype(object).fillna("")


def parse_money(value: Any) -> float:
    """Parse a price cell into a float. Returns NaN when there is no number."""
    if value is None:
        return float("nan")
    if isinstance(value, (int, float, np.integer, np.floating)):
        f = float(value)
        return float("nan") if math.isnan(f) else f
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-", "na", "n/a"):
        return float("nan")
    negative = s.startswith("(") and s.endswith(")")
    s = _MONEY_STRIP.sub("", s)
    if s in ("", "-", ".", "-."):
        return float("nan")
    try:
        f = float(s)
    except ValueError:
        return float("nan")
    return -f if negative else f


def money_series(s: pd.Series) -> pd.Series:
    """Vectorised parse_money."""
    txt = s.astype("string").fillna("").str.strip()
    txt = txt.str.replace(r"[^\d.\-]", "", regex=True)
    txt = txt.where(~txt.isin(["", "-", ".", "-."]), None)
    return pd.to_numeric(txt, errors="coerce").astype("float64")


def _canon_header(name: Any) -> str:
    """Canonical form of a header name for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def resolve_columns(available: list, wanted: list, where: str) -> dict:
    """Map required logical names to the file's real headers, case-insensitively."""
    lookup = {}
    for col in available:
        lookup.setdefault(_canon_header(col), col)
    mapping = {}
    missing = []
    for want in wanted:
        real = lookup.get(_canon_header(want))
        if real is None:
            missing.append(want)
        else:
            mapping[want] = real
    if missing:
        raise UserError(
            f"{where} is missing required column(s): {', '.join(missing)}. "
            f"Columns found in the file: {', '.join(map(str, available)) or '(none)'}."
        )
    return mapping


def fmt_money(v) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if math.isnan(f):
        return ""
    return f"{f:,.2f}"


# ---------------------------------------------------------------------------
# File sniffing (used to populate the UI dropdowns)
# ---------------------------------------------------------------------------

def is_excel(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".xlsx", ".xlsm", ".xls")


def csv_opts(path: str) -> dict:
    """Sniff the delimiter and encoding of a text file.

    csv.Sniffer / pandas' sep=None guess from the first line, which supplier
    files ruin with a title row like 'DENSO INDIA PRIVATE LIMITED'. Instead we
    pick the delimiter whose per-line count is the most consistent.
    """
    encoding = "utf-8-sig"
    try:
        with open(path, "rb") as fh:
            raw = fh.read(262144)
    except OSError:
        return {"sep": ",", "encoding": encoding}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = raw.decode("cp1252", errors="replace")

    lines = [ln for ln in text.splitlines() if ln.strip()][:50]
    best_sep, best_score = ",", (0, 0)
    for cand in (",", ";", "\t", "|"):
        counts = [ln.count(cand) for ln in lines]
        nonzero = [c for c in counts if c > 0]
        if not nonzero:
            continue
        modal = max(set(nonzero), key=nonzero.count)
        score = (nonzero.count(modal), modal)
        if score > best_score:
            best_score, best_sep = score, cand
    return {"sep": best_sep, "encoding": encoding}


def csv_width(path: str, sep: str) -> int:
    """Widest record in the file, in fields.

    Supplier CSVs are ragged: a one-cell title row followed by three-cell data
    rows. With header=None pandas would size the frame from the first line and
    then reject every wider row, so we measure the file first and hand pandas
    an explicit set of column positions.
    """
    sep_b = sep.encode("utf-8")
    width = 1
    try:
        with open(path, "rb") as fh:
            for line in fh:
                n = line.count(sep_b) + 1
                if n > width:
                    width = n
    except OSError:
        return 64
    return width + 2  # margin for embedded newlines in quoted fields


def _raw_head(path: str, n: int = 30, sheet=0) -> pd.DataFrame:
    """Read the first n rows with no header at all, everything as text."""
    try:
        if is_excel(path):
            return pd.read_excel(path, sheet_name=sheet, header=None, nrows=n, dtype=str)
        opts = csv_opts(path)
        return pd.read_csv(
            path,
            header=None,
            names=range(csv_width(path, opts["sep"])),
            nrows=n,
            dtype=str,
            keep_default_na=False,
            skip_blank_lines=False,
            on_bad_lines="skip",
            **opts,
        )
    except UserError:
        raise
    except Exception as exc:  # unreadable / corrupt / wrong format
        raise UserError(
            f"Could not read '{os.path.basename(path)}' as a CSV or Excel file: {exc}"
        ) from exc


def excel_sheets(path: str) -> list:
    if not is_excel(path):
        return []
    try:
        with pd.ExcelFile(path) as xl:
            return [str(s) for s in xl.sheet_names]
    except Exception as exc:
        raise UserError(f"Could not open workbook '{os.path.basename(path)}': {exc}") from exc


def detect_header_row(head: pd.DataFrame) -> int:
    """Guess which row holds the real headers (supplier files bury them under junk)."""
    best_row, best_score = 0, -1.0
    for i in range(len(head)):
        row = head.iloc[i]
        cells = [str(c).strip() for c in row.tolist()
                 if str(c).strip() not in ("", "nan", "None")]
        if not cells:
            continue
        distinct = len(set(c.lower() for c in cells))
        # Headers are text, not numbers.
        texty = sum(1 for c in cells if not re.fullmatch(r"-?[\d,.]+", c))
        score = distinct + texty * 0.5 - i * 0.01
        if score > best_score:
            best_score, best_row = score, i
    return best_row


def header_names(head: pd.DataFrame, header_row: int) -> list:
    """Column names as they appear on header_row, blanks filled in positionally."""
    if header_row >= len(head):
        return []
    names, seen = [], {}
    for idx, raw in enumerate(head.iloc[header_row].tolist()):
        name = str(raw).strip()
        if name in ("", "nan", "None"):
            name = f"Column {idx + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        names.append(name)
    return names


_PART_HINTS = ["partnumber", "partno", "partcode", "material", "materialcode", "itemcode",
               "sku", "articleno", "articlenumber", "oeno", "code", "part"]
_DESC_HINTS = ["partdescription", "description", "partname", "materialdescription",
               "itemname", "productname", "name", "desc"]
_MRP_HINTS = ["mrp", "newmrp", "mrpinrs", "listprice", "price", "rate", "mrprs", "retailprice"]


def _guess(columns: list, hints: list):
    canon = [(c, _canon_header(c)) for c in columns]
    for hint in hints:                      # exact canonical match wins
        for original, c in canon:
            if c == hint:
                return original
    for hint in hints:                      # then substring
        for original, c in canon:
            if hint in c:
                return original
    return None


def guess_mapping(columns: list) -> dict:
    part = _guess(columns, _PART_HINTS)
    desc = _guess([c for c in columns if c != part], _DESC_HINTS)
    mrp = _guess([c for c in columns if c not in (part, desc)], _MRP_HINTS)
    return {"part_col": part, "desc_col": desc, "mrp_col": mrp}


def sniff_file(path: str, sheet=0, header_row=None) -> dict:
    """Everything the UI needs to render the column-mapping dropdowns."""
    sheets = excel_sheets(path)
    if is_excel(path) and (sheet is None or sheet == 0) and sheets:
        sheet = sheets[0]
    head = _raw_head(path, 30, sheet)
    if head.empty:
        raise UserError(f"'{os.path.basename(path)}' appears to be empty - no rows found.")
    hdr = detect_header_row(head) if header_row is None else int(header_row)
    hdr = max(0, min(hdr, len(head) - 1))
    cols = header_names(head, hdr)
    preview = []
    for i in range(hdr + 1, min(hdr + 6, len(head))):
        preview.append(["" if str(v).strip() in ("nan", "None") else str(v)
                        for v in head.iloc[i].tolist()])
    choices = []
    for i in range(min(len(head), 15)):
        text = " | ".join(str(v) for v in head.iloc[i].tolist()[:8]
                          if str(v).strip() not in ("", "nan", "None"))
        choices.append({"index": i, "text": text[:110] or "(blank row)"})
    return {
        "columns": cols,
        "header_row": hdr,
        "header_row_choices": choices,
        "preview": preview,
        "sheets": sheets,
        "sheet": sheet if isinstance(sheet, str) else None,
        "guess": guess_mapping(cols),
    }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

@dataclass
class LoadResult:
    df: pd.DataFrame
    rows_read: int = 0
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def load_inventory(path: str, chunksize: int = 100_000) -> LoadResult:
    """Read the inventory master in chunks so a 50MB+ file never blows up memory."""
    name = os.path.basename(path)
    if is_excel(path):
        raw = pd.read_excel(path, dtype=str)
        cols = list(raw.columns)
        mapping = resolve_columns(cols, INVENTORY_REQUIRED, f"Inventory file '{name}'")
        chunks = [raw]
    else:
        opts = csv_opts(path)
        try:
            header = pd.read_csv(path, nrows=0, **opts)
        except pd.errors.EmptyDataError:
            raise UserError(f"Inventory file '{name}' is empty.")
        except Exception as exc:
            raise UserError(f"Could not read inventory file '{name}': {exc}") from exc
        cols = list(header.columns)
        mapping = resolve_columns(cols, INVENTORY_REQUIRED, f"Inventory file '{name}'")
        usecols = list({mapping[c] for c in INVENTORY_REQUIRED})
        chunks = pd.read_csv(path, usecols=usecols, dtype=str, keep_default_na=False,
                             chunksize=chunksize, on_bad_lines="skip", **opts)

    frames = []
    rows_read = 0
    for chunk in chunks:
        rows_read += len(chunk)
        part_key = norm_series(chunk[mapping["part_no"]])
        keep = part_key != ""
        if not keep.any():
            continue
        sub = chunk[keep]
        piece = pd.DataFrame({
            "part_key": part_key[keep].to_numpy(),
            "part_no": sub[mapping["part_no"]].astype("string").fillna("").str.strip().to_numpy(),
            "brand_key": norm_series(sub[mapping["brand"]]).to_numpy(),
            "brand": sub[mapping["brand"]].astype("string").fillna("").str.strip().to_numpy(),
            "part_name": sub[mapping["part_name"]].astype("string").fillna("").str.strip().to_numpy(),
            "mrp_inventory": money_series(sub[mapping["mrp"]]).to_numpy(),
        })
        frames.append(piece)

    if frames:
        df = pd.concat(frames, ignore_index=True)
    else:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in
                           ["part_key", "part_no", "brand_key", "brand", "part_name"]})
        df["mrp_inventory"] = pd.Series(dtype="float64")

    warnings = []
    blank_parts = rows_read - len(df)
    if blank_parts > 0:
        warnings.append(f"Inventory: skipped {blank_parts:,} row(s) with no usable part number.")
    before = len(df)
    df = df.drop_duplicates(subset=["brand_key", "part_key"], keep="first").reset_index(drop=True)
    dupes = before - len(df)
    if dupes > 0:
        warnings.append(
            f"Inventory: {dupes:,} duplicate brand+part_no row(s) found - kept the first of each."
        )
    if df.empty:
        raise UserError(
            f"Inventory file '{name}' has no usable rows (every row had a blank part number)."
        )
    brands = sorted({b for b in df["brand"].tolist() if b})
    return LoadResult(df=df, rows_read=rows_read, warnings=warnings,
                      stats={"unique_parts": len(df), "brands": brands})


def load_mrp_list(path: str, brand: str, part_col: str, desc_col, mrp_col: str,
                  sheet=0, header_row: int = 0, display_name: str = "") -> LoadResult:
    """Read one supplier MRP sheet using the column mapping the user picked."""
    name = display_name or os.path.basename(path)
    header_row = int(header_row or 0)
    if not str(brand).strip():
        raise UserError(f"MRP file '{name}': pick which brand this list is for.")
    try:
        if is_excel(path):
            if (sheet is None or sheet == 0):
                sheets = excel_sheets(path)
                sheet = sheets[0] if sheets else 0
            raw = pd.read_excel(path, sheet_name=sheet, header=None,
                                skiprows=header_row, dtype=str)
        else:
            opts = csv_opts(path)
            raw = pd.read_csv(path, header=None,
                              names=range(csv_width(path, opts["sep"])),
                              skiprows=header_row, dtype=str,
                              keep_default_na=False, skip_blank_lines=False,
                              on_bad_lines="skip", **opts)
    except UserError:
        raise
    except Exception as exc:
        raise UserError(f"Could not read MRP file '{name}': {exc}") from exc

    if raw.empty:
        raise UserError(f"MRP file '{name}' has no rows at or below the chosen header row.")

    names = header_names(raw, 0)
    raw = raw.iloc[1:].reset_index(drop=True)
    raw.columns = names

    where = f"MRP list for {brand} ('{name}'"
    where += f", sheet '{sheet}'" if isinstance(sheet, str) else ""
    where += f", header on row {header_row + 1})"
    for label, col in (("part number", part_col), ("MRP", mrp_col)):
        if col not in names:
            others = excel_sheets(path)
            hint = (f" Other sheets in this workbook: {', '.join(s for s in others if s != sheet)}."
                    if len(others) > 1 else "")
            raise UserError(
                f"{where}: the chosen {label} column '{col}' is not one of the headers "
                f"actually read there ({', '.join(names)}).{hint} Re-select the file, "
                f"sheet, header row and columns for this list, then run again."
            )
    if desc_col and desc_col not in names:
        desc_col = None

    rows_read = len(raw)
    desc = (raw[desc_col].astype("string").fillna("").str.strip()
            if desc_col else pd.Series([""] * len(raw), dtype="string"))
    df = pd.DataFrame({
        "part_key": norm_series(raw[part_col]).to_numpy(),
        "part_no": raw[part_col].astype("string").fillna("").str.strip().to_numpy(),
        "description": desc.to_numpy(),
        "mrp_official": money_series(raw[mrp_col]).to_numpy(),
        "_mrp_txt": raw[mrp_col].astype("string").fillna("").str.strip().to_numpy(),
    })

    dropped = {"no_part_no": 0, "header_repeat": 0, "all_blank_or_zero": 0, "duplicates": 0}

    # 1. Rows that simply repeat the header text in the mapped columns.
    header_tokens = {norm_key(part_col), norm_key(mrp_col)}
    if desc_col:
        header_tokens.add(norm_key(desc_col))
    header_tokens.discard("")
    is_header_repeat = (
        df["part_key"].isin(header_tokens)
        | norm_series(df["description"]).isin(header_tokens)
        | norm_series(df["_mrp_txt"]).isin(header_tokens)
    )
    dropped["header_repeat"] = int(is_header_repeat.sum())
    df = df[~is_header_repeat]

    # 2. Rows where every mapped cell is blank or a literal 0 (trailing filler).
    def _blank_or_zero(s: pd.Series) -> pd.Series:
        t = pd.Series(norm_series(s), index=s.index).astype("string").fillna("")
        return (t == "") | t.str.fullmatch(r"-?0+(\.0+)?").fillna(False)

    all_junk = (_blank_or_zero(df["part_no"]) & _blank_or_zero(df["description"])
                & _blank_or_zero(df["_mrp_txt"]))
    dropped["all_blank_or_zero"] = int(all_junk.sum())
    df = df[~all_junk]

    # 3. Rows with no usable part number.
    no_part = df["part_key"] == ""
    dropped["no_part_no"] = int(no_part.sum())
    df = df[~no_part]

    # 4. Duplicate part numbers inside this one list: keep the first.
    before = len(df)
    df = df.drop_duplicates(subset=["part_key"], keep="first").reset_index(drop=True)
    dropped["duplicates"] = before - len(df)

    df["mrp_is_blank"] = df["_mrp_txt"].astype("string").fillna("").str.strip() == ""
    df = df.drop(columns=["_mrp_txt"])
    df["brand"] = str(brand).strip()
    df["brand_key"] = norm_key(brand)

    warnings = []
    junk = dropped["no_part_no"] + dropped["header_repeat"] + dropped["all_blank_or_zero"]
    if junk:
        warnings.append(
            f"MRP '{name}' ({brand}): stripped {junk:,} junk row(s) - "
            f"{dropped['no_part_no']:,} with no part number, "
            f"{dropped['header_repeat']:,} repeating the header text, "
            f"{dropped['all_blank_or_zero']:,} all-blank/all-zero filler rows."
        )
    if dropped["duplicates"]:
        warnings.append(
            f"MRP '{name}' ({brand}): {dropped['duplicates']:,} duplicate part number(s) in "
            f"this list - kept the first occurrence of each, dropped the rest."
        )
    if df.empty:
        raise UserError(
            f"MRP file '{name}' ({brand}) had no usable rows left after junk rows were "
            f"stripped. Check the header row and the column mapping."
        )
    return LoadResult(df=df, rows_read=rows_read, warnings=warnings,
                      stats={"usable_rows": len(df), "dropped": dropped})


def _empty_stock() -> pd.DataFrame:
    return pd.DataFrame({
        "brand_key": pd.Series(dtype="object"),
        "part_key": pd.Series(dtype="object"),
        "available_quantity": pd.Series(dtype="float64"),
    })


def load_stock(path, chunksize: int = 200_000) -> LoadResult:
    """Read warehouse stock. Missing or empty is allowed - it degrades, not fails."""
    if not path:
        return LoadResult(df=_empty_stock(), rows_read=0,
                          stats={"available": False, "has_brand": False})

    name = os.path.basename(path)
    if os.path.getsize(path) == 0:
        return LoadResult(df=_empty_stock(), rows_read=0,
                          warnings=[f"Stock file '{name}' is a 0-byte file."],
                          stats={"available": False, "has_brand": False})
    try:
        if is_excel(path):
            raw_all = pd.read_excel(path, dtype=str)
            cols = list(raw_all.columns)
        else:
            opts = csv_opts(path)
            cols = list(pd.read_csv(path, nrows=0, **opts).columns)
            raw_all = None
    except pd.errors.EmptyDataError:
        return LoadResult(df=_empty_stock(), rows_read=0,
                          warnings=[f"Stock file '{name}' contains no columns or rows."],
                          stats={"available": False, "has_brand": False})
    except Exception as exc:
        raise UserError(f"Could not read stock file '{name}': {exc}") from exc

    mapping = resolve_columns(cols, STOCK_REQUIRED, f"Stock file '{name}'")
    brand_real = next((c for c in cols if _canon_header(c) == "brand"), None)

    warnings = []
    if brand_real is None:
        warnings.append(
            f"Stock file '{name}' has no 'brand' column - stock is matched on part number "
            f"alone, which can merge identical part numbers belonging to different brands."
        )

    usecols = list({mapping["part_no"], mapping["available_quantity"]}
                   | ({brand_real} if brand_real else set()))
    if raw_all is not None:
        chunks = [raw_all]
    else:
        chunks = pd.read_csv(path, usecols=usecols, dtype=str, keep_default_na=False,
                             chunksize=chunksize, on_bad_lines="skip", **opts)

    frames, rows_read = [], 0
    for chunk in chunks:
        rows_read += len(chunk)
        part_key = norm_series(chunk[mapping["part_no"]])
        keep = part_key != ""
        if not keep.any():
            continue
        sub = chunk[keep]
        qty_txt = (sub[mapping["available_quantity"]].astype("string").fillna("")
                   .str.replace(r"[^\d.\-]", "", regex=True))
        qty_txt = qty_txt.where(~qty_txt.isin(["", "-", ".", "-."]), None)
        frames.append(pd.DataFrame({
            "part_key": part_key[keep].to_numpy(),
            "brand_key": (norm_series(sub[brand_real]).to_numpy() if brand_real
                          else np.array([""] * len(sub), dtype=object)),
            "available_quantity": pd.to_numeric(qty_txt, errors="coerce").fillna(0.0).to_numpy(),
        }))

    if not frames:
        return LoadResult(df=_empty_stock(), rows_read=rows_read,
                          warnings=warnings + [f"Stock file '{name}' has no usable rows."],
                          stats={"available": False, "has_brand": brand_real is not None})

    df = pd.concat(frames, ignore_index=True)
    # Sum across warehouses / repeated rows for the same part.
    df = df.groupby(["brand_key", "part_key"], as_index=False)["available_quantity"].sum()
    return LoadResult(df=df, rows_read=rows_read, warnings=warnings,
                      stats={"available": True, "has_brand": brand_real is not None,
                             "unique_parts": len(df)})


# ---------------------------------------------------------------------------
# Step 1 - Inventory vs MRP list, per brand
# ---------------------------------------------------------------------------

def run_step1(inventory: pd.DataFrame, mrp_lists: list):
    """Classify every part of every uploaded brand into exactly one bucket."""
    if not mrp_lists:
        raise UserError("Add at least one brand MRP list before running the check.")

    by_brand = {}
    merged_warnings = []
    for frame in mrp_lists:
        bk = frame["brand_key"].iloc[0]
        if bk in by_brand:
            combined = pd.concat([by_brand[bk], frame], ignore_index=True)
            before = len(combined)
            combined = combined.drop_duplicates(subset=["part_key"], keep="first").reset_index(drop=True)
            if before - len(combined):
                merged_warnings.append(
                    f"Brand '{frame['brand'].iloc[0]}' has more than one MRP list uploaded - "
                    f"they were combined and {before - len(combined):,} part number(s) present "
                    f"in both were kept once (first list wins)."
                )
            by_brand[bk] = combined
        else:
            by_brand[bk] = frame

    out_frames = []
    ok_count = 0
    per_brand = {}

    for bk, mrp in by_brand.items():
        brand_display = mrp["brand"].iloc[0]
        inv = inventory[inventory["brand_key"] == bk]
        m = inv.merge(
            mrp[["part_key", "part_no", "description", "mrp_official", "mrp_is_blank"]],
            on="part_key", how="outer", suffixes=("_inv", "_mrp"), indicator=True,
        ).reset_index(drop=True)

        inv_part = m["part_no_inv"].astype(object) if "part_no_inv" in m else pd.Series([None] * len(m))
        mrp_part = m["part_no_mrp"].astype(object) if "part_no_mrp" in m else pd.Series([None] * len(m))
        part_no = inv_part.where(inv_part.notna() & (inv_part != ""), mrp_part)

        inv_name = m["part_name"].astype(object)
        part_name = inv_name.where(inv_name.notna() & (inv_name != ""), m["description"].astype(object))

        inv_mrp = pd.to_numeric(m["mrp_inventory"], errors="coerce").astype("float64")
        off_mrp = pd.to_numeric(m["mrp_official"], errors="coerce").astype("float64")
        blank = m["mrp_is_blank"].fillna(False).astype(bool)
        both = (m["_merge"] == "both").to_numpy()
        left_only = (m["_merge"] == "left_only").to_numpy()
        right_only = (m["_merge"] == "right_only").to_numpy()

        issue = pd.Series([""] * len(m), index=m.index, dtype=object)
        direction = pd.Series([""] * len(m), index=m.index, dtype=object)
        note = pd.Series([""] * len(m), index=m.index, dtype=object)

        issue[left_only] = ISSUE_MISSING_IN_MRP
        note[left_only] = (
            "Part is in Inventory but not on the uploaded MRP list for this brand - "
            "possibly discontinued by the supplier; verify before de-listing."
        )
        issue[right_only] = ISSUE_MISSING_IN_INV
        note[right_only] = (
            "Part is on the new MRP list but not set up in Inventory - create the part master."
        )

        # A literal 0 (or a blank cell) on the official list is never a price cut.
        zero = both & (blank.to_numpy() | (off_mrp.fillna(-1.0).abs() < MRP_TOLERANCE).to_numpy())
        issue[zero] = ISSUE_MRP_ZERO
        note[zero & blank.to_numpy()] = BLANK_NOTE
        note[zero & ~blank.to_numpy()] = ZERO_NOTE

        comparable = both & ~zero
        inv_for_compare = inv_mrp.fillna(0.0)
        differs = comparable & ((inv_for_compare - off_mrp).abs() > MRP_TOLERANCE).to_numpy()
        issue[differs] = ISSUE_MRP_MISMATCH
        up = differs & (off_mrp > inv_for_compare).to_numpy()
        direction[up] = "INCREASE"
        direction[differs & ~up] = "DECREASE"

        if differs.any():
            # Built column-at-a-time: a per-row .iloc loop here costs ~30s on a
            # 250k-issue run, this costs well under a second.
            idxs = np.flatnonzero(differs)
            old = inv_for_compare.to_numpy()[idxs]
            new = off_mrp.to_numpy()[idxs]
            delta = np.abs(new - old)
            base = np.abs(old)
            pct = np.where(base > 0, delta / np.where(base > 0, base, 1.0) * 100.0, np.nan)
            money = "{:,.2f}".format
            note.iloc[idxs] = (
                "Inventory MRP " + pd.Series(old).map(money)
                + " vs official " + pd.Series(new).map(money)
                + " - " + pd.Series(np.where(up[idxs], "increase", "decrease"))
                + " of " + pd.Series(delta).map(money)
                + pd.Series(pct).map(lambda p: "" if p != p else f" ({p:.1f}%)")
                + "."
                + pd.Series(np.where(np.isnan(inv_mrp.to_numpy()[idxs]),
                                     " Inventory MRP was blank and treated as 0.", ""))
            ).to_numpy()

        ok_mask = comparable & ~differs
        ok_count += int(ok_mask.sum())
        issue[ok_mask] = MATCHED_SAME_MRP
        note[ok_mask] = "Inventory MRP already matches the official MRP list."

        result = pd.DataFrame({
            "brand": brand_display,
            "part_no": part_no.fillna("").astype(object),
            "part_name": part_name.fillna("").astype(object),
            "issue_type": issue,
            "mrp_inventory": inv_mrp,
            "mrp_official": off_mrp,
            "mrp_direction": direction,
            "note": note,
            "_part_key": m["part_key"].astype(object),
            "_brand_key": bk,
        })
        if not result.empty:
            out_frames.append(result)

        per_brand[brand_display] = {
            "inventory_parts": int(len(inv)),
            "mrp_list_parts": int(len(mrp)),
            "ok": int(ok_mask.sum()),
            # How many of the list's parts were found in Inventory at all. A zero
            # here almost always means the wrong file, brand or part-number column.
            "matched": int(both.sum()),
        }

    if out_frames:
        classified = pd.concat(out_frames, ignore_index=True)
    else:
        classified = pd.DataFrame({c: pd.Series(dtype="object")
                                   for c in STEP1_COLUMNS + ["_part_key", "_brand_key"]})
        classified["mrp_inventory"] = pd.Series(dtype="float64")
        classified["mrp_official"] = pd.Series(dtype="float64")

    if not classified.empty:
        classified = classified.sort_values(["brand", "issue_type", "part_no"],
                                            kind="stable").reset_index(drop=True)

    # Step 1 reports issues only. Parts that already agree, and parts the
    # supplier prices at 0/blank, stay in `classified` for the per-brand lists
    # but never reach the issue reports.
    step1 = classified[~classified["issue_type"].isin(
        (MATCHED_SAME_MRP,) + NOT_AN_ISSUE)].reset_index(drop=True)

    covered = set(by_brand)
    skipped = sorted({b for b, k in zip(inventory["brand"], inventory["brand_key"])
                      if k not in covered and b})
    summary = {
        "per_brand": per_brand,
        "matched_ok": ok_count,
        "skipped_brands": skipped,
        "warnings": merged_warnings,
    }
    return step1, summary, classified


# ---------------------------------------------------------------------------
# Per-brand lists: same MRP / MRP changed / new part
# ---------------------------------------------------------------------------

def _empty_list(key: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in LIST_SPECS[key]["columns"]})


def split_lists(classified: pd.DataFrame) -> dict:
    """Split one run into the four per-brand lists.

    Every usable row of every uploaded MRP list lands in exactly one of them,
    so the four row counts always add up to the rows read off the lists.
    Parts that are in Inventory but absent from the supplier's list are not
    here at all - they are a Step 1 concern (MISSING_IN_MRP_LIST).
    """
    out = {k: _empty_list(k) for k in LIST_KEYS}
    if classified.empty:
        return out

    def base(df):
        return {"brand": df["brand"].to_numpy(),
                "part_no": df["part_no"].to_numpy(),
                "part_name": df["part_name"].to_numpy()}

    same = classified[classified["issue_type"] == MATCHED_SAME_MRP]
    if not same.empty:
        out["same"] = pd.DataFrame({**base(same), "mrp": same["mrp_official"].to_numpy()})

    chg = classified[classified["issue_type"] == ISSUE_MRP_MISMATCH]
    if not chg.empty:
        old = pd.to_numeric(chg["mrp_inventory"], errors="coerce").fillna(0.0).to_numpy()
        new = pd.to_numeric(chg["mrp_official"], errors="coerce").to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = np.where(np.abs(old) > 0, (new - old) / np.where(np.abs(old) > 0, old, 1.0) * 100.0,
                           np.nan)
        out["changed"] = pd.DataFrame({
            **base(chg),
            "mrp_old": old,
            "mrp_new": new,
            "mrp_direction": chg["mrp_direction"].to_numpy(),
            "mrp_difference": new - old,
            "mrp_change_pct": pct,
        })

    fresh = classified[classified["issue_type"] == ISSUE_MISSING_IN_INV]
    if not fresh.empty:
        out["new"] = pd.DataFrame({
            **base(fresh),
            "mrp_new": fresh["mrp_official"].to_numpy(),
            "status": "NEW PART - not present in Inventory, needs a part master",
        })

    zero = classified[classified["issue_type"] == ISSUE_MRP_ZERO]
    if not zero.empty:
        out["zero"] = pd.DataFrame({
            **base(zero),
            "mrp_inventory": zero["mrp_inventory"].to_numpy(),
            "note": zero["note"].to_numpy(),
        })

    return out


# ---------------------------------------------------------------------------
# Step 2 - filter the mismatches against stock
# ---------------------------------------------------------------------------

def run_step2(step1: pd.DataFrame, stock: pd.DataFrame, stock_available: bool,
              stock_has_brand: bool):
    df = step1.copy()
    if df.empty:
        out = pd.DataFrame({c: pd.Series(dtype="object") for c in STEP2_COLUMNS})
        return out, {"dropped": 0, "kept": 0}

    if stock_available and not stock.empty:
        if stock_has_brand:
            qty_map = stock.set_index(["brand_key", "part_key"])["available_quantity"]
            idx = pd.MultiIndex.from_arrays([df["_brand_key"].astype(object),
                                             df["_part_key"].astype(object)])
            qty = pd.Series(qty_map.reindex(idx).to_numpy(), index=df.index, dtype="float64")
        else:
            by_part = stock.groupby("part_key", as_index=True)["available_quantity"].sum()
            qty = pd.Series(by_part.reindex(df["_part_key"].astype(object)).to_numpy(),
                            index=df.index, dtype="float64")
    else:
        qty = pd.Series(np.nan, index=df.index, dtype="float64")

    df["available_quantity"] = qty
    in_stock = (qty.fillna(0.0) > 0) & bool(stock_available)
    df["in_stock"] = np.where(in_stock, "YES", "NO")
    df["stock_data_available"] = "YES" if stock_available else "NO"

    is_mismatch = (df["issue_type"] == ISSUE_MRP_MISMATCH).to_numpy()
    increase = (df["mrp_direction"] == "INCREASE").to_numpy()
    in_stock_np = in_stock.to_numpy()

    drop_mask = is_mismatch & ~in_stock_np & increase

    reason = pd.Series([""] * len(df), index=df.index, dtype=object)
    reason[~is_mismatch] = (
        df.loc[~is_mismatch, "issue_type"].astype(str)
        + " has no old/new MRP pair to weigh against stock -> passed through unchanged"
    )
    kept_in_stock = is_mismatch & ~drop_mask & in_stock_np
    if kept_in_stock.any():
        qty_txt = qty.fillna(0.0).map(lambda v: f"{v:g}")
        verb = df.loc[kept_in_stock, "mrp_direction"].map(
            {"INCREASE": "raised", "DECREASE": "reduced"}).astype(str)
        reason[kept_in_stock] = ("in stock (qty=" + qty_txt[kept_in_stock]
                                 + ") + MRP " + verb
                                 + " -> physical stock needs re-pricing/re-labelling -> issue")
    kept_no_stock = is_mismatch & ~drop_mask & ~in_stock_np
    reason[kept_no_stock] = (
        "not in stock + MRP reduced -> issue" if stock_available
        else "no stock data (assumed not in stock) + MRP reduced -> issue"
    )

    df["decision_reason"] = reason
    dropped = int(drop_mask.sum())
    out = df[~drop_mask].reset_index(drop=True)
    stats = {
        "dropped": dropped,
        "kept": int(len(out)),
        "dropped_reason": "not in stock + MRP increased -> no physical stock is mispriced",
    }
    return out, stats
