"""
Generate sample input files so the app can be tried (and tested) without real data.

    python make_sample_data.py              -> small, readable files in sample_data/
    python make_sample_data.py --big 250000 -> also a 250k-row inventory (~50MB+)

The samples deliberately include the messy things real supplier files contain:
junk header rows, repeated header rows mid-file, trailing all-zero filler rows,
duplicate part numbers, blank MRP cells and literal 0 MRPs.
"""

from __future__ import annotations

import argparse
import csv
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sample_data")

BRANDS = ["BOSCH", "DENSO", "NGK"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    print(f"  wrote {os.path.relpath(path, HERE)} ({os.path.getsize(path):,} bytes)")


def small_sample():
    os.makedirs(OUT, exist_ok=True)

    # ---------------------------------------------------------- inventory
    inventory = [["part_no", "brand", "part_name", "mrp"]]
    inventory += [
        # BOSCH ---------------------------------------------------------
        ["0986AB1234", "BOSCH", "Brake pad set front", "2450.00"],   # mismatch INCREASE, in stock
        ["0986AB1235", "BOSCH", "Brake pad set rear", "2100.00"],    # mismatch INCREASE, no stock -> dropped
        ["0986AB1236", "BOSCH", "Oil filter", "  340.50 "],          # mismatch DECREASE, no stock -> kept
        ["0986ab1237", "BOSCH", "Air filter", "510.00"],             # lowercase + mismatch DECREASE, in stock
        ["0986AB1238", "BOSCH", "Spark plug FR7", "180.00"],         # identical -> no issue
        ["0986AB1239", "BOSCH", "Wiper blade 22in", "690.00"],       # official list shows 0
        ["0986AB1240", "BOSCH", "Horn disc 12V", "1150.00"],         # not on the MRP list
        # DENSO ---------------------------------------------------------
        ["DEN-55110", "DENSO", "Radiator assembly", "8750.00"],      # mismatch INCREASE, in stock
        ["DEN-55111", "DENSO", "Condenser", "6400.00"],              # identical
        ["DEN-55112", "DENSO", "Blower motor", "4300.00"],           # mismatch DECREASE, no stock
        ["den-55113", "DENSO", "Cabin filter", "450.00"],            # blank MRP on official list
        ["DEN-55114", "DENSO", "Compressor clutch", "12500.00"],     # not on the MRP list
        # NGK - inventory only, no MRP list uploaded for this brand -----
        ["NGK-BKR6E", "NGK", "Spark plug BKR6E", "210.00"],
        ["NGK-LKR7A", "NGK", "Spark plug LKR7A bulk", "260.00"],
        ["", "NGK", "junk row with no part number", "0"],
    ]
    write_csv(os.path.join(OUT, "inventory.csv"), inventory)

    # ------------------------------------------- BOSCH MRP list (tidy CSV)
    bosch = [["PART NUMBER", "Part Description", "MRP"]]
    bosch += [
        ["0986AB1234", "BRAKE PAD SET FRONT", "2695.00"],
        ["0986AB1235", "BRAKE PAD SET REAR", "2310.00"],
        ["0986AB1236", "OIL FILTER", "299.00"],
        ["0986AB1237", "AIR FILTER", "465.00"],
        ["0986AB1238", "SPARK PLUG FR7", "180.00"],
        ["0986AB1239", "WIPER BLADE 22IN", "0"],
        ["0986AB1241", "FUEL PUMP MODULE", "7850.00"],   # not in inventory
        ["0986AB1299", "SEALANT DRUM BULK", "0"],        # new part, no usable MRP -> ignored
        ["0986AB1236", "OIL FILTER (DUPLICATE ROW)", "305.00"],  # duplicate -> dropped
        ["", "", ""],
        ["0", "0", "0"],
    ]
    write_csv(os.path.join(OUT, "mrp_bosch.csv"), bosch)

    # ------------------- DENSO MRP list (junk header rows + filler rows)
    denso = [
        ["DENSO INDIA PRIVATE LIMITED"],
        ["OFFICIAL MRP LIST - EFFECTIVE 01.04.2026"],
        ["(All prices inclusive of GST)"],
        [],
        ["Material", "Description", "MRP"],
        ["DEN-55110", "RADIATOR ASSEMBLY", "9,250.00"],
        ["DEN-55111", "CONDENSER", "6400.00"],
        ["DEN-55112", "BLOWER MOTOR", "3,990.00"],
        ["DEN-55113", "CABIN FILTER", ""],
        ["DEN-55115", "EXPANSION VALVE", "2150.00"],     # not in inventory
        ["Material", "Description", "MRP"],              # repeated header mid-file
        ["DEN-55116", "RECEIVER DRIER", "1875.00"],      # not in inventory
        ["0", "0", "0"],
        ["0", "0", "0"],
        ["", "", ""],
    ]
    write_csv(os.path.join(OUT, "mrp_denso.csv"), denso)

    # ------------------------------------------------------------- stock
    stock = [["part_no", "available_quantity", "brand"]]
    stock += [
        ["0986AB1234", "12", "BOSCH"],       # in stock, hike -> kept
        ["0986AB1234", "3", "BOSCH"],        # second warehouse, summed
        ["0986AB1235", "0", "BOSCH"],        # zero -> not in stock, hike -> dropped
        ["0986AB1237", "7", "BOSCH"],        # in stock, cut -> kept
        ["DEN-55110", "2", "DENSO"],         # in stock, hike -> kept
        ["DEN-55112", "0", "DENSO"],         # not in stock, cut -> kept
    ]
    write_csv(os.path.join(OUT, "stock.csv"), stock)

    write_csv(os.path.join(OUT, "stock_empty.csv"), [["part_no", "available_quantity", "brand"]])
    with open(os.path.join(OUT, "stock_zero_bytes.csv"), "w", encoding="utf-8"):
        pass
    print("  wrote sample_data/stock_zero_bytes.csv (0 bytes)")


def xlsx_sample():
    """An XLSX MRP list with a decoy first sheet and letterhead above the header."""
    try:
        from openpyxl import Workbook
    except ImportError:
        print("  (openpyxl not installed - skipping the XLSX sample)")
        return
    os.makedirs(OUT, exist_ok=True)
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["A1"] = "NGK SPARK PLUGS - PRICE CIRCULAR"
    cover["A2"] = "Do not use this sheet"

    ws = wb.create_sheet("Price List")
    for row in [
        ["NGK SPARK PLUGS (INDIA) PVT LTD"],
        ["REVISED MRP - EFFECTIVE 01.04.2026"],
        [],
        ["Item Code", "Item Name", "MRP (Rs.)"],
        ["NGK-BKR6E", "SPARK PLUG BKR6E", 235],
        ["NGK-BKR5E", "SPARK PLUG BKR5E", 225],
        ["NGK-LKR7A", "SPARK PLUG LKR7A (BULK)", 0],
        ["Item Code", "Item Name", "MRP (Rs.)"],
        [0, 0, 0],
        [None, None, None],
    ]:
        ws.append(row)
    path = os.path.join(OUT, "mrp_ngk.xlsx")
    wb.save(path)
    print(f"  wrote {os.path.relpath(path, HERE)} ({os.path.getsize(path):,} bytes)")


def dupe_prone_sample():
    """A list big enough to judge a column by: its description column repeats
    heavily, so picking it as the part number should be flagged."""
    os.makedirs(OUT, exist_ok=True)
    rows = [["PART NUMBER", "Part Description", "MRP"]]
    families = ["OIL FILTER", "AIR FILTER", "BRAKE PAD SET", "SPARK PLUG",
                "WIPER BLADE", "FUEL FILTER", "CABIN FILTER", "CLUTCH PLATE"]
    for i in range(40):
        rows.append([f"DUP-{i:05d}", families[i % len(families)], f"{500 + i * 7}.00"])
    write_csv(os.path.join(OUT, "mrp_dupe_prone.csv"), rows)


def two_sheet_sample():
    """A workbook whose two sheets use different column names, like a real
    supplier file where 'Accessories' and 'Spare Parts' are laid out differently."""
    try:
        from openpyxl import Workbook
    except ImportError:
        print("  (openpyxl not installed - skipping the two-sheet sample)")
        return
    os.makedirs(OUT, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Accessories"
    ws.append(["Part No", "Description", "Div", "New MRP"])
    ws.append(["0603BAB0080KT", "PAD KIT - BRAKE FRONT", "AD", 6577])
    ws.append(["S0305D020021N", "FILTER ELEMENT CATRIDGE WITH O RING", "AD", 783])
    ws2 = wb.create_sheet("Spare Parts")
    ws2.append(["Part Number", "Description", "Old MRP", "New MRP"])
    ws2.append(["MAH-1001", "CLUTCH PLATE", 4000, 4400])
    path = os.path.join(OUT, "mrp_two_sheet.xlsx")
    wb.save(path)
    print(f"  wrote {os.path.relpath(path, HERE)} ({os.path.getsize(path):,} bytes)")


def big_inventory(rows: int):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "inventory_big.csv")
    rnd = random.Random(7)
    names = ["Brake pad set", "Oil filter", "Air filter", "Spark plug", "Wiper blade",
             "Radiator", "Condenser", "Blower motor", "Fuel pump", "Clutch plate",
             "Shock absorber", "Timing belt kit", "Water pump", "Alternator", "Starter motor"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["part_no", "brand", "part_name", "mrp"])
        for i in range(rows):
            brand = BRANDS[i % len(BRANDS)]
            w.writerow([
                f"{brand[:3]}-{i:08d}",
                brand,
                f"{rnd.choice(names)} type {rnd.randint(100, 999)} for model "
                f"{rnd.choice('ABCDEFGH')}{rnd.randint(10, 99)} variant {rnd.randint(1, 9)}",
                f"{rnd.randint(50, 50000)}.{rnd.randint(0, 99):02d}",
            ])
    print(f"  wrote {os.path.relpath(path, HERE)} "
          f"({os.path.getsize(path) / 1024 / 1024:.1f} MB, {rows:,} rows)")

    # A matching MRP list for one brand, where ~12% of prices moved.
    mrp_path = os.path.join(OUT, "mrp_big_bosch.csv")
    with open(mrp_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["PART NUMBER", "Part Description", "MRP"])
        rnd2 = random.Random(7)
        for i in range(rows):
            brand = BRANDS[i % len(BRANDS)]
            name = (f"{rnd2.choice(names)} type {rnd2.randint(100, 999)} for model "
                    f"{rnd2.choice('ABCDEFGH')}{rnd2.randint(10, 99)} variant {rnd2.randint(1, 9)}")
            base = rnd2.randint(50, 50000)
            cents = rnd2.randint(0, 99)
            if brand != "BOSCH":
                continue
            roll = rnd2.random()
            if roll < 0.06:
                price = f"{int(base * 1.1)}.{cents:02d}"     # hike
            elif roll < 0.12:
                price = f"{int(base * 0.9)}.{cents:02d}"     # cut
            elif roll < 0.13:
                price = "0"                                  # bulk pack
            else:
                price = f"{base}.{cents:02d}"                # unchanged
            w.writerow([f"{brand[:3]}-{i:08d}", name.upper(), price])
        for _ in range(500):
            w.writerow(["0", "0", "0"])
    print(f"  wrote {os.path.relpath(mrp_path, HERE)} "
          f"({os.path.getsize(mrp_path) / 1024 / 1024:.1f} MB)")

    stock_path = os.path.join(OUT, "stock_big.csv")
    with open(stock_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["part_no", "available_quantity", "brand"])
        rnd3 = random.Random(11)
        for i in range(0, rows, 3):          # BOSCH rows only
            if rnd3.random() < 0.4:
                w.writerow([f"BOS-{i:08d}", rnd3.randint(1, 40), "BOSCH"])
    print(f"  wrote {os.path.relpath(stock_path, HERE)} "
          f"({os.path.getsize(stock_path) / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--big", type=int, default=0,
                    help="also generate a large inventory with this many rows")
    a = ap.parse_args()
    print("Generating sample data:")
    small_sample()
    xlsx_sample()
    two_sheet_sample()
    dupe_prone_sample()
    if a.big:
        big_inventory(a.big)
    print("Done.")
