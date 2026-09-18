"""
End-to-end self test. Generates the sample files, runs both steps through the
HTTP API exactly as the browser does, and asserts every rule from the spec.

    python self_test.py
"""

from __future__ import annotations

import io
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_sample_data                      # noqa: E402
import mrp_core as core                      # noqa: E402
from app import app                          # noqa: E402

SD = os.path.join(HERE, "sample_data")
FAILED = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(label)


def upload(client, endpoint, path):
    r = client.post(f"/api/{endpoint}", data={"path": path})
    body = r.get_json()
    assert body.get("ok"), f"{endpoint} failed: {body}"
    return body


def rows_of(csv_text):
    return pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)


def run(client, mrp_specs, stock_path):
    inv = upload(client, "inventory", os.path.join(SD, "inventory.csv"))
    stock_id = None
    if stock_path:
        stock_id = upload(client, "stock", stock_path)["file_id"]
    specs = []
    for fname, brand, part, desc, mrp in mrp_specs:
        info = upload(client, "mrp", os.path.join(SD, fname))
        specs.append({
            "file_id": info["file_id"], "brand": brand,
            "part_col": part, "desc_col": desc, "mrp_col": mrp,
            "header_row": info["header_row"], "sheet": info.get("sheet"),
        })
    r = client.post("/api/run", json={
        "inventory_file_id": inv["file_id"],
        "stock_file_id": stock_id,
        "mrp_lists": specs,
    })
    body = r.get_json()
    assert body.get("ok"), f"run failed: {body}"
    s1 = rows_of(client.get(f"/api/download/{body['run_id']}/step1").get_data(as_text=True))
    s2 = rows_of(client.get(f"/api/download/{body['run_id']}/step2").get_data(as_text=True))
    return body, s1, s2, info


def val(row, key):
    """Safe cell read for failure messages (a pandas row is never truthy)."""
    return "" if row is None else str(row[key])


def get(df, *part_nos):
    """First row whose part_no matches any of the given spellings, else None."""
    for part_no in part_nos:
        hit = df[df["part_no"].str.upper() == part_no.upper()]
        if len(hit):
            return hit.iloc[0]
    return None


def main():
    print("Generating sample data...")
    make_sample_data.small_sample()
    make_sample_data.xlsx_sample()

    client = app.test_client()
    specs = [
        ("mrp_bosch.csv", "BOSCH", "PART NUMBER", "Part Description", "MRP"),
        ("mrp_denso.csv", "DENSO", "Material", "Description", "MRP"),
    ]

    # ------------------------------------------------ header auto-detection
    print("\nHeader / column detection")
    info = upload(client, "mrp", os.path.join(SD, "mrp_denso.csv"))
    check("DENSO junk header rows skipped (header found on row 5)", info["header_row"] == 4,
          f"got row index {info['header_row']}")
    check("DENSO columns read from the real header",
          info["columns"][:3] == ["Material", "Description", "MRP"], str(info["columns"]))
    check("column mapping auto-guessed",
          info["guess"]["part_col"] == "Material" and info["guess"]["mrp_col"] == "MRP",
          str(info["guess"]))

    # ------------------------------------------------ full run with stock
    print("\nRun WITH stock data")
    body, s1, s2, _ = run(client, specs, os.path.join(SD, "stock.csv"))

    check("step1 CSV header matches the spec",
          list(s1.columns) == core.STEP1_COLUMNS, str(list(s1.columns)))
    check("step2 CSV header matches the spec",
          list(s2.columns) == core.STEP2_COLUMNS, str(list(s2.columns)))

    print("\nStep 1 - classification")
    r = get(s1, "0986AB1234")
    check("hike is MRP_MISMATCH / INCREASE",
          r is not None and r["issue_type"] == "MRP_MISMATCH" and r["mrp_direction"] == "INCREASE")
    r = get(s1, "0986AB1236")
    check("cut is MRP_MISMATCH / DECREASE",
          r is not None and r["issue_type"] == "MRP_MISMATCH" and r["mrp_direction"] == "DECREASE")
    check("identical MRP produces no issue row", get(s1, "0986AB1238") is None)
    zero_csv = rows_of(client.get(
        f"/api/download/{body['run_id']}/zero").get_data(as_text=True))
    check("a part the supplier prices at 0 is NOT an issue in Step 1",
          get(s1, "0986AB1239") is None)
    r = get(zero_csv, "0986AB1239")
    check("it appears in the 'No MRP on list' list instead", r is not None)
    check("that list keeps the explanatory note",
          r is not None and "likely a bulk/non-retail pack" in r["note"])
    check("a 0 on the list is never reported as a price cut",
          "MRP_LIST_SHOWS_ZERO" not in set(s1["issue_type"])
          and get(s1, "0986AB1239") is None)
    check("Step 1 now reports only actionable issue types",
          set(s1["issue_type"]) <= set(core.REPORTED_ISSUE_TYPES), str(set(s1["issue_type"])))
    r = get(s1, "0986AB1240")
    check("inventory-only part is MISSING_IN_MRP_LIST",
          r is not None and r["issue_type"] == "MISSING_IN_MRP_LIST")
    r = get(s1, "0986AB1241")
    check("list-only part is MISSING_IN_INVENTORY",
          r is not None and r["issue_type"] == "MISSING_IN_INVENTORY")
    check("list-only part carries the supplier description",
          r is not None and "FUEL PUMP" in r["part_name"].upper())

    print("\nStep 1 - normalisation, junk rows, dedupe, brand isolation")
    r = get(s1, "0986ab1237", "0986AB1237")
    check("lowercase inventory part matched the uppercase list part",
          r is not None and r["issue_type"] == "MRP_MISMATCH")
    check("first duplicate MRP row wins (299.00, not 305.00)",
          get(s1, "0986AB1236")["mrp_official"] == "299.00")
    bosch_file = [f for f in body["mrp_files"] if f["brand"] == "BOSCH"][0]
    check("duplicate part number counted and reported",
          bosch_file["dropped"]["duplicates"] == 1, str(bosch_file["dropped"]))
    check("all-blank / all-zero filler rows stripped",
          bosch_file["dropped"]["all_blank_or_zero"] >= 2, str(bosch_file["dropped"]))
    denso_file = [f for f in body["mrp_files"] if f["brand"] == "DENSO"][0]
    check("repeated header row stripped",
          denso_file["dropped"]["header_repeat"] == 1, str(denso_file["dropped"]))
    check("no cross-brand matching (NGK never appears)",
          "NGK" not in set(s1["brand"]), str(set(s1["brand"])))
    check("brand with no MRP list is reported as skipped",
          body["skipped_brands"] == ["NGK"], str(body["skipped_brands"]))
    r = get(zero_csv, "den-55113", "DEN-55113")
    check("a blank MRP cell lands in the same list, not reported as a cut",
          r is not None and get(s1, "den-55113", "DEN-55113") is None,
          val(r, "note"))
    check("comma-formatted price parsed (9,250.00)",
          get(s1, "DEN-55110")["mrp_official"] == "9250.00")

    print("\nStep 2 - stock filter")
    check("not in stock + INCREASE is dropped", get(s2, "0986AB1235") is None)
    check("dropped count reported", body["step2"]["dropped"] == 1, str(body["step2"]["dropped"]))
    r = get(s2, "0986AB1236")
    check("not in stock + DECREASE is kept", r is not None)
    check("decision_reason explains the keep",
          r is not None and r["decision_reason"] == "not in stock + MRP reduced -> issue",
          val(r, "decision_reason"))
    r = get(s2, "0986AB1234")
    check("in stock + INCREASE is kept", r is not None)
    check("quantities summed across warehouses (12 + 3 = 15)",
          r is not None and r["available_quantity"] == "15", val(r, "available_quantity"))
    check("in-stock row flagged YES", r is not None and r["in_stock"] == "YES")
    check("in-stock reason mentions re-pricing",
          r is not None and "re-pricing" in r["decision_reason"], val(r, "decision_reason"))
    r = get(s2, "0986AB1237")
    check("in stock + DECREASE is kept", r is not None)
    check("non-mismatch issues pass through untouched",
          get(s2, "0986AB1240") is not None and get(s2, "0986AB1241") is not None)
    check("zero-MRP parts never reach Step 2 either", get(s2, "0986AB1239") is None)
    check("pass-through rows say why they were not stock-filtered",
          "passed through unchanged" in get(s2, "0986AB1240")["decision_reason"])
    check("stock_data_available is YES on every row",
          set(s2["stock_data_available"]) == {"YES"}, str(set(s2["stock_data_available"])))
    check("step2 total == step1 total - dropped",
          len(s2) == len(s1) - body["step2"]["dropped"], f"{len(s2)} vs {len(s1)}")
    check("the run reports how many parts were set aside for having no MRP",
          body["no_mrp_on_list"] == len(zero_csv) and body["no_mrp_on_list"] > 0,
          str(body.get("no_mrp_on_list")))
    check("the four lists still account for every usable MRP-list row",
          body["lists"]["same"]["total"] + body["lists"]["changed"]["total"]
          + body["lists"]["new"]["total"] + body["lists"]["zero"]["total"]
          == sum(f["usable_rows"] for f in body["mrp_files"]))

    # ------------------------------------------------ no stock file at all
    print("\nRun with NO stock file")
    body2, s1b, s2b = run(client, specs, None)[:3]
    check("warning banner text is returned",
          body2["stock_warning"] and "No stock data was available" in body2["stock_warning"])
    check("stock_data_available is NO", set(s2b["stock_data_available"]) == {"NO"})
    check("all hikes dropped when stock is unknown",
          s2b[(s2b["issue_type"] == "MRP_MISMATCH")
              & (s2b["mrp_direction"] == "INCREASE")].empty)
    check("cuts survive when stock is unknown",
          not s2b[(s2b["issue_type"] == "MRP_MISMATCH")
                  & (s2b["mrp_direction"] == "DECREASE")].empty)
    check("other issue types still pass through", get(s2b, "0986AB1240") is not None)

    # ------------------------------------------------ empty / 0-byte stock
    print("\nRun with an EMPTY stock file (header only) and a 0-byte file")
    body3 = run(client, specs, os.path.join(SD, "stock_empty.csv"))[0]
    check("header-only stock file is a warning, not a failure",
          body3["ok"] and body3["stock_data_available"] is False)
    body4 = run(client, specs, os.path.join(SD, "stock_zero_bytes.csv"))[0]
    check("0-byte stock file is a warning, not a failure",
          body4["ok"] and body4["stock_data_available"] is False)

    # ------------------------------------------------ error messages
    print("\nError handling")
    bad = os.path.join(SD, "bad_inventory.csv")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("colA,colB,colC\nX1,BOSCH,Thing\n")
    r = client.post("/api/inventory", data={"path": bad}).get_json()
    check("missing required columns produces a clear message",
          not r["ok"] and "missing required column" in r["error"]
          and "part_no" in r["error"] and "mrp" in r["error"], str(r))

    # ------------------------------------------------ column-name synonyms
    print()
    print("Column-name synonyms (stock/inventory exports rarely use the exact name)")
    syn_stock = os.path.join(SD, "stock_synonym.csv")
    with open(syn_stock, "w", encoding="utf-8") as fh:
        fh.write("part_no,part_name,quantity,mrp,brand,warehouse_name\n"
                 "P1,Widget,15,100,BOSCH,BIJWASAN\nP1,Widget,5,100,BOSCH,ANOTHER\n")
    r = client.post("/api/stock", data={"path": syn_stock}).get_json()
    check("a stock file using 'quantity' instead of 'available_quantity' is accepted",
          r["ok"] and r["available"], str(r))
    check("quantities from the synonym column are still summed correctly (15+5)",
          r["unique_parts"] == 1, str(r))
    check("using a synonym column is reported as a warning, not left silent",
          any("used column 'quantity' as 'available_quantity'" in w for w in r["warnings"]),
          str(r["warnings"]))

    syn_inv = os.path.join(SD, "inventory_synonym.csv")
    with open(syn_inv, "w", encoding="utf-8") as fh:
        fh.write("code,maker,description,price\nX1,BOSCH,Thing,99.00\n")
    r = client.post("/api/inventory", data={"path": syn_inv}).get_json()
    check("an inventory file with no exact column names, only synonyms, is still accepted",
          r["ok"], str(r))
    check("all four inventory columns resolved via synonym are reported",
          len(r["warnings"]) >= 4, str(r["warnings"]))

    r = client.post("/api/inventory", data={"path": os.path.join(SD, "nope.csv")}).get_json()
    check("missing file produces a clear message", not r["ok"] and "No file found" in r["error"])
    weird = os.path.join(SD, "notes.pdf")
    open(weird, "wb").write(b"%PDF-1.4 not really")
    r = client.post("/api/mrp", data={"path": weird}).get_json()
    check("unsupported file type is rejected clearly",
          not r["ok"] and "unsupported extension" in r["error"], str(r))
    r = client.post("/api/run", json={"inventory_file_id": None, "mrp_lists": []}).get_json()
    check("running without inventory is refused", not r["ok"])

    # ------------------------------------------------ pagination + filters
    print("\nPagination, filters and sorting")
    body5, _, _, _ = run(client, specs, os.path.join(SD, "stock.csv"))
    rid = body5["run_id"]
    p = client.get(f"/api/results/{rid}/step1?page=1&page_size=10").get_json()
    check("page size honoured (and total is bigger than one page)",
          len(p["rows"]) == 10 and p["pages"] >= 2, f"{len(p['rows'])} rows, {p['pages']} pages")
    p2 = client.get(f"/api/results/{rid}/step1?page=2&page_size=10").get_json()
    check("page 2 returns different rows", p["rows"][0]["part_no"] != p2["rows"][0]["part_no"])
    pz = client.get(f"/api/results/{rid}/step1?page=99&page_size=10").get_json()
    check("out-of-range page clamps to the last page", pz["page"] == pz["pages"])
    p3 = client.get(f"/api/results/{rid}/step1?brand=DENSO&page_size=500").get_json()
    check("brand filter works", {r["brand"] for r in p3["rows"]} == {"DENSO"})
    p4 = client.get(f"/api/results/{rid}/step1?issue_type=MRP_MISMATCH&page_size=500").get_json()
    check("issue_type filter works", {r["issue_type"] for r in p4["rows"]} == {"MRP_MISMATCH"})
    p5 = client.get(f"/api/results/{rid}/step1?sort=part_no&dir=desc&page_size=500").get_json()
    parts = [r["part_no"] for r in p5["rows"]]
    check("sorting works", parts == sorted(parts, reverse=True))
    dl = client.get(f"/api/download/{rid}/step2?filtered=1&brand=BOSCH")
    check("filtered download respects the brand filter",
          set(rows_of(dl.get_data(as_text=True))["brand"]) == {"BOSCH"})
    check("download sets a filename",
          "step2_final_issues_after_stock_check.csv" in dl.headers["Content-Disposition"])

    # ------------------------------------------------ per-brand lists
    print()
    print("Per-brand lists: same MRP / MRP changed / new parts")
    lb, _, _, _ = run(client, specs, os.path.join(SD, "stock.csv"))
    rid2 = lb["run_id"]

    def list_csv(key):
        return rows_of(client.get(f"/api/download/{rid2}/{key}").get_data(as_text=True))

    same = list_csv("same")
    check("same-MRP list has the identical-price parts only",
          set(same["part_no"]) == {"0986AB1238", "DEN-55111"}, str(set(same["part_no"])))
    check("same-MRP list columns", list(same.columns) == ["brand", "part_no", "part_name", "mrp"],
          str(list(same.columns)))
    check("same-MRP list carries the agreed price",
          get(same, "0986AB1238")["mrp"] == "180.00", val(get(same, "0986AB1238"), "mrp"))

    changed = list_csv("changed")
    check("changed list columns include old and new MRP",
          list(changed.columns) == ["brand", "part_no", "part_name", "mrp_old", "mrp_new",
                                    "mrp_direction", "mrp_difference", "mrp_change_pct"],
          str(list(changed.columns)))
    r = get(changed, "0986AB1234")
    check("changed row shows old 2450.00 -> new 2695.00",
          r is not None and r["mrp_old"] == "2450.00" and r["mrp_new"] == "2695.00",
          val(r, "mrp_old") + " -> " + val(r, "mrp_new"))
    check("changed row records direction and signed difference",
          r is not None and r["mrp_direction"] == "INCREASE" and r["mrp_difference"] == "245.00",
          val(r, "mrp_difference"))
    r = get(changed, "0986AB1236")
    check("a price cut is a negative difference",
          r is not None and r["mrp_direction"] == "DECREASE"
          and r["mrp_difference"] == "-41.50", val(r, "mrp_difference"))
    check("changed list is NOT stock-filtered (the dropped hike is still here)",
          get(changed, "0986AB1235") is not None)

    fresh = list_csv("new")
    check("new-parts list holds only parts absent from Inventory",
          set(fresh["part_no"]) == {"0986AB1241", "DEN-55115", "DEN-55116"},
          str(set(fresh["part_no"])))
    check("new-parts list says they are new",
          all("NEW PART" in s for s in fresh["status"]), str(set(fresh["status"])))
    check("new-parts list carries the supplier description and price",
          get(fresh, "0986AB1241")["part_name"] == "FUEL PUMP MODULE"
          and get(fresh, "0986AB1241")["mrp_new"] == "7850.00")

    zero = list_csv("zero")
    check("zero/blank MRP parts kept out of same and changed",
          set(zero["part_no"]) == {"0986AB1239", "den-55113"}, str(set(zero["part_no"])))

    total = len(same) + len(changed) + len(fresh) + len(zero)
    usable = sum(f["usable_rows"] for f in lb["mrp_files"])
    check("the four lists account for every usable MRP-list row",
          total == usable, f"{total} listed vs {usable} usable")
    check("run summary reports the list counts",
          lb["lists"]["same"]["total"] == len(same)
          and lb["lists"]["changed"]["total"] == len(changed)
          and lb["lists"]["new"]["total"] == len(fresh), str(lb["lists"]))
    check("lists are brand-filterable",
          {r["brand"] for r in client.get(
              f"/api/results/{rid2}/changed?brand=DENSO&page_size=500").get_json()["rows"]}
          == {"DENSO"})
    dl = client.get(f"/api/download/{rid2}/new")
    check("new-parts download is named list3_...",
          "list3_new_parts_not_in_inventory.csv" in dl.headers["Content-Disposition"],
          dl.headers["Content-Disposition"])

    # ------------------------------------------------ XLSX MRP list
    print("\nXLSX MRP list (multi-sheet, letterhead rows, numeric cells)")
    xl = upload(client, "mrp", os.path.join(SD, "mrp_ngk.xlsx"))
    check("workbook sheets listed", xl["sheets"] == ["Cover", "Price List"], str(xl["sheets"]))
    sniff = client.post("/api/mrp/resniff", json={
        "file_id": xl["file_id"], "sheet": "Price List", "header_row": None}).get_json()
    check("header row found under the letterhead rows", sniff["header_row"] == 3,
          str(sniff["header_row"]))
    check("XLSX columns read", sniff["columns"][:3] == ["Item Code", "Item Name", "MRP (Rs.)"],
          str(sniff["columns"]))
    check("XLSX mapping auto-guessed",
          sniff["guess"]["part_col"] == "Item Code" and sniff["guess"]["mrp_col"] == "MRP (Rs.)",
          str(sniff["guess"]))
    inv = upload(client, "inventory", os.path.join(SD, "inventory.csv"))
    xr = client.post("/api/run", json={
        "inventory_file_id": inv["file_id"],
        "stock_file_id": None,
        "mrp_lists": [{"file_id": xl["file_id"], "brand": "NGK", "sheet": "Price List",
                       "header_row": sniff["header_row"],
                       "part_col": "Item Code", "desc_col": "Item Name",
                       "mrp_col": "MRP (Rs.)"}],
    }).get_json()
    assert xr.get("ok"), xr
    xs1 = rows_of(client.get(f"/api/download/{xr['run_id']}/step1").get_data(as_text=True))
    r = get(xs1, "NGK-BKR6E")
    check("XLSX numeric MRP cell compared correctly",
          r is not None and r["issue_type"] == "MRP_MISMATCH"
          and r["mrp_official"] == "235.00" and r["mrp_direction"] == "INCREASE",
          val(r, "issue_type") + "/" + val(r, "mrp_official"))
    xzero = rows_of(client.get(
        f"/api/download/{xr['run_id']}/zero").get_data(as_text=True))
    check("an XLSX literal 0 goes to the 'No MRP on list' list, not the issues",
          get(xzero, "NGK-LKR7A") is not None and get(xs1, "NGK-LKR7A") is None)
    ngk_file = xr["mrp_files"][0]
    check("XLSX repeated header + zero filler rows stripped",
          ngk_file["dropped"]["header_repeat"] == 1
          and ngk_file["dropped"]["all_blank_or_zero"] >= 1, str(ngk_file["dropped"]))

    # ------------------------------------------------ wrong file / wrong sheet
    print()
    print("Guards against a stale file, sheet or brand on an MRP block")
    make_sample_data.two_sheet_sample()
    inv2 = upload(client, "inventory", os.path.join(SD, "inventory.csv"))
    two = upload(client, "mrp", os.path.join(SD, "mrp_two_sheet.xlsx"))
    check("a multi-sheet workbook sniffs its first sheet",
          two["sheet"] == "Accessories" and two["columns"][:2] == ["Part No", "Description"],
          f"{two['sheet']} {two['columns']}")

    def two_run(**over):
        spec = {"file_id": two["file_id"], "brand": "VEEDOL", "sheet": two["sheet"],
                "header_row": two["header_row"], "part_col": "Part No",
                "desc_col": "Description", "mrp_col": "New MRP"}
        spec.update(over)
        return client.post("/api/run", json={"inventory_file_id": inv2["file_id"],
                                             "stock_file_id": None,
                                             "mrp_lists": [spec]}).get_json()

    r = two_run(sheet="Spare Parts")
    check("a mapping that belongs to another sheet is refused, not run", not r["ok"])
    check("the refusal names the brand, the file and the sheet",
          "VEEDOL" in r["error"] and "mrp_two_sheet.xlsx" in r["error"]
          and "Spare Parts" in r["error"], r.get("error", ""))
    check("the refusal lists the headers actually found there",
          "Part Number, Description, Old MRP, New MRP" in r["error"], r.get("error", ""))
    check("the refusal never shows the internal temp filename",
          "_mrp_two_sheet" not in r["error"], r.get("error", ""))

    r = two_run(part_col="No Such Column")
    check("a column that is not in the file is refused", not r["ok"])

    r = two_run()
    check("the correct sheet still runs", r["ok"], r.get("error", ""))

    bos = upload(client, "mrp", os.path.join(SD, "mrp_bosch.csv"))
    r = client.post("/api/run", json={
        "inventory_file_id": inv2["file_id"], "stock_file_id": None,
        "mrp_lists": [{"file_id": bos["file_id"], "brand": "VEEDOL", "part_col": "PART NUMBER",
                       "desc_col": "Part Description", "mrp_col": "MRP", "header_row": 0}],
    }).get_json()
    check("a list whose parts match nothing under that brand raises the alarm",
          r["ok"] and len(r["brand_mismatch"]) == 1, str(r.get("brand_mismatch")))
    check("the alarm says how many part numbers failed to match",
          "not one of the 7 part numbers" in r["brand_mismatch"][0], str(r["brand_mismatch"]))
    check("every part of a wrong-brand list reads as new",
          r["lists"]["new"]["total"] == 7 and r["lists"]["same"]["total"] == 0,
          str(r["lists"]["new"]["total"]))

    r = client.post("/api/run", json={
        "inventory_file_id": inv2["file_id"], "stock_file_id": None,
        "mrp_lists": [{"file_id": bos["file_id"], "brand": "BOSCH", "part_col": "PART NUMBER",
                       "desc_col": "Part Description", "mrp_col": "MRP", "header_row": 0}],
    }).get_json()
    check("no alarm when the brand really does match", r["brand_mismatch"] == [],
          str(r["brand_mismatch"]))

    # ------------------------------------------------ frontend guards
    print()
    print("Frontend guards (static checks on the served app.js)")
    appjs = client.get("/static/app.js").get_data(as_text=True)
    check("setStatus keeps an element's hook classes (classList, not className)",
          ".className = `status" not in appjs and "node.classList.add(kind)" in appjs,
          "overwriting className drops .mrp-status and the block can never reload")
    check("choosing another file resets that MRP block",
          "function resetMrpEntry" in appjs and "resetMrpEntry(entry," in appjs)
    check("Run check is blocked while an MRP list is still being read",
          "m.busy" in appjs and "Still reading an MRP list" in appjs)
    check("a list that failed to re-read blocks the run",
          "entry.error = true" in appjs and "did not read cleanly" in appjs)
    check("the mapping is re-checked against the loaded file before running",
          "function validateMrpBlocks" in appjs and "validateMrpBlocks()" in appjs)
    check("the wrong-brand alarm is rendered", "brand_mismatch" in appjs)

    # ------------------------------------------------ upload folder lifecycle
    print()
    print("Upload folder lifecycle")
    import app as appmod
    import shutil as _shutil

    check("each instance gets its own upload folder",
          appmod.UPLOAD_DIR.startswith(appmod.UPLOAD_ROOT)
          and appmod.UPLOAD_DIR != appmod.UPLOAD_ROOT
          and str(os.getpid()) in appmod.UPLOAD_DIR, appmod.UPLOAD_DIR)
    check("pruning never touches this instance's own folder",
          (appmod.prune_old_upload_dirs(0) or True) and os.path.isdir(appmod.UPLOAD_DIR))

    with open(os.path.join(SD, "inventory.csv"), "rb") as fh:
        up = client.post("/api/inventory",
                         data={"file": (io.BytesIO(fh.read()), "inventory.csv")},
                         content_type="multipart/form-data").get_json()
    check("a real multipart upload works", up["ok"] and up["unique_parts"] > 0)
    held = appmod.FILES[up["file_id"]]["path"]
    check("the upload lands in this instance's folder", held.startswith(appmod.UPLOAD_DIR))

    _shutil.rmtree(appmod.UPLOAD_DIR, ignore_errors=True)
    with open(os.path.join(SD, "inventory.csv"), "rb") as fh:
        again = client.post("/api/inventory",
                            data={"file": (io.BytesIO(fh.read()), "inventory.csv")},
                            content_type="multipart/form-data").get_json()
    check("an upload recreates the folder if something deleted it", again["ok"],
          str(again.get("error")))

    mrp_ok = upload(client, "mrp", os.path.join(SD, "mrp_bosch.csv"))
    os.remove(appmod.FILES[again["file_id"]]["path"])   # the temp copy disappears
    resp = client.post("/api/run", json={
        "inventory_file_id": again["file_id"], "stock_file_id": None,
        "mrp_lists": [{"file_id": mrp_ok["file_id"], "brand": "BOSCH",
                       "part_col": "PART NUMBER", "desc_col": "Part Description",
                       "mrp_col": "MRP", "header_row": 0}]})
    body_ = resp.get_json()
    check("a vanished upload is a clear 400, never a 500 traceback",
          resp.status_code == 400 and not body_["ok"]
          and "no longer readable" in body_["error"], f"{resp.status_code} {body_}")
    check("that message tells the user what to do",
          "Re-select the file" in body_["error"], body_["error"])

    resp = client.post("/api/run", json={
        "inventory_file_id": "deadbeefcafe", "stock_file_id": None,
        "mrp_lists": [{"file_id": mrp_ok["file_id"], "brand": "BOSCH",
                       "part_col": "PART NUMBER", "desc_col": "Part Description",
                       "mrp_col": "MRP", "header_row": 0}]})
    check("a file_id from a previous server run is a clear 400",
          resp.status_code == 400 and "app is restarted" in resp.get_json()["error"],
          str(resp.get_json()))

    check("every response carries the server id the page checks",
          client.get("/").headers.get("X-MRP-Server") == appmod.SERVER_ID)
    check("the page reacts to a server restart",
          "checkServerRestart" in appjs and "X-MRP-Server" in appjs
          and "no longer has your files" in appjs)

    # ------------------------------------------------ column-sanity guards
    print()
    print("Column sanity guards")
    inv3 = upload(client, "inventory", os.path.join(SD, "inventory.csv"))
    bos3 = upload(client, "mrp", os.path.join(SD, "mrp_bosch.csv"))

    def bos_run(part_col, mrp_col="MRP", desc_col="Part Description"):
        return client.post("/api/run", json={
            "inventory_file_id": inv3["file_id"], "stock_file_id": None,
            "mrp_lists": [{"file_id": bos3["file_id"], "brand": "BOSCH",
                           "part_col": part_col, "desc_col": desc_col,
                           "mrp_col": mrp_col, "header_row": 0}]}).get_json()

    r = bos_run("MRP")
    check("one column cannot be both the part number and the MRP", not r["ok"])
    check("that refusal names the offending column and says what to do",
          "'MRP' selected as BOTH" in r["error"] and "part numbers" in r["error"],
          r.get("error", ""))

    make_sample_data.dupe_prone_sample()
    dup = upload(client, "mrp", os.path.join(SD, "mrp_dupe_prone.csv"))
    r = client.post("/api/run", json={
        "inventory_file_id": inv3["file_id"], "stock_file_id": None,
        "mrp_lists": [{"file_id": dup["file_id"], "brand": "BOSCH",
                       "part_col": "Part Description", "desc_col": "PART NUMBER",
                       "mrp_col": "MRP", "header_row": 0}]}).get_json()
    check("a part column that is mostly duplicates is flagged",
          r["ok"] and len(r["suspect_mapping"]) == 1, str(r.get("suspect_mapping")))
    check("the flag reports the duplicate share",
          "does not look like a part-number column" in r["suspect_mapping"][0]
          and "duplicates" in r["suspect_mapping"][0], str(r["suspect_mapping"]))

    r = bos_run("PART NUMBER")
    check("the correct mapping raises neither flag",
          r["ok"] and not r["suspect_mapping"] and not r["brand_mismatch"],
          f"{r.get('suspect_mapping')} {r.get('brand_mismatch')}")
    check("the client blocks part==MRP before sending",
          "both the part-number column" in appjs, "missing client-side guard")

    # ------------------------------------------------ hosted-mode safety
    print()
    print("Hosted-mode safety (deploying to a server)")
    import base64 as _b64, subprocess as _sp, sys as _sys

    def child(env_extra, code):
        env = dict(os.environ, **env_extra)
        prelude = "import sys; sys.path.insert(0, r'" + HERE + "'); "
        return _sp.run([_sys.executable, "-c", prelude + code],
                       capture_output=True, text=True, env=env)

    out = child({"MRP_HOSTED": "1", "MRP_PASSWORD": ""}, "import app")
    check("a hosted deploy without a password refuses to start",
          out.returncode != 0 and "refuses to start" in out.stderr, out.stderr[-120:])
    check("that refusal explains the exposure risk",
          "expose your inventory" in out.stderr, out.stderr[-120:])

    out = child({"MRP_HOSTED": "1", "MRP_PASSWORD": "pw"},
                "import app; print(app.IS_HOSTED, app.ALLOW_LOCAL_PATHS, bool(app.AUTH_PASSWORD))")
    check("with a password it starts, with local paths off",
          out.returncode == 0 and out.stdout.strip() == "True False True",
          out.stdout.strip() + out.stderr[-120:])

    out = child({"MRP_HOSTED": "1", "MRP_PASSWORD": "pw", "MRP_ALLOW_LOCAL_PATHS": "1"},
                "import app; print(app.ALLOW_LOCAL_PATHS)")
    check("path reading can be switched back on deliberately",
          out.stdout.strip() == "True", out.stdout.strip())

    out = child({}, "import app; print(app.IS_HOSTED, app.ALLOW_LOCAL_PATHS, bool(app.AUTH_PASSWORD))")
    check("a local run stays password-free with paths enabled",
          out.stdout.strip() == "False True False", out.stdout.strip())

    check("this local test client needs no password", client.get("/").status_code == 200)
    check("the mode endpoint reports local mode",
          client.get("/api/mode").get_json() ==
          {"ok": True, "hosted": False, "allow_local_paths": True, "auth": False},
          str(client.get("/api/mode").get_json()))
    check("the page swaps the offline badge when hosted",
          "hosted \u00b7 files are uploaded" in appjs and "/api/mode" in appjs)

    for f, must in [("render.yaml", ["--workers 1", "MRP_PASSWORD", "healthCheckPath"]),
                    ("requirements.txt", ["gunicorn"]),
                    ("DEPLOY.md", ["MRP_PASSWORD", "Render"])]:
        path = os.path.join(HERE, f)
        body_txt = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        check(f"{f} exists and covers the essentials",
              bool(body_txt) and all(m in body_txt for m in must), f"missing: {f}")

    # ------------------------------------------------ frontend: tab race / downloads
    print()
    print("Frontend: tab-switch race guard and explicit download filenames")
    check("load() is guarded against a stale, out-of-order response overwriting a newer tab",
          "requestSeq" in appjs and "mySeq !== requestSeq" in appjs)
    check("the redundant duplicate load of the lists table on run completion is gone",
          appjs.count("tables.lists.load()") == 1,
          f"found {appjs.count('tables.lists.load()')} calls - expected exactly 1 (inside renderListTabs)")
    check("every result set has an explicit client-side download filename",
          all(name in appjs for name in [
              "step1_inventory_vs_mrp_issues.csv", "step2_final_issues_after_stock_check.csv",
              "list1_matched_same_mrp.csv", "list2_mrp_changed_old_vs_new.csv",
              "list3_new_parts_not_in_inventory.csv", "list4_mrp_list_shows_zero.csv"]))
    check("the download anchor's filename is not left blank",
          "download: '' }" not in appjs and "download: filename" in appjs)

    # ------------------------------------------------ offline guarantees
    print("\nOffline guarantees")
    html = client.get("/").get_data(as_text=True)
    js = client.get("/static/app.js").get_data(as_text=True)
    css = client.get("/static/app.css").get_data(as_text=True)
    blob = html + js + css
    for needle in ("http://", "https://", "//cdn", "googleapis", "unpkg", "jsdelivr"):
        check(f"no '{needle}' reference in the served frontend", needle not in blob)
    check("CSP header restricts everything to 'self'",
          "default-src 'self'" in client.get("/").headers.get("Content-Security-Policy", ""))

    print("\n" + "=" * 62)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
