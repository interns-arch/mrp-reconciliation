# MRP Reconciliation

A local, fully offline web app that reconciles a company's product **Inventory**
against supplier **MRP price lists** and current warehouse **Stock**.

Nothing leaves the machine. The page loads only its own CSS/JS from `localhost`;
there are no CDNs, no web fonts, no analytics, no outbound requests of any kind.
All parsing and matching happens in the Python process against files you choose.

---

## Run it

```bash
python -m pip install -r requirements.txt
```

```bash
python app.py
```

Then open <http://127.0.0.1:8765/> (it opens automatically). `Ctrl+C` stops it.

Options: `--port 9000`, `--host 0.0.0.0`, `--no-browser`, `--debug`.

On Windows you can also just double-click `run.bat`.

### Try it without real data

```bash
python make_sample_data.py
```

That writes `sample_data/` with deliberately messy files: junk header rows, a
repeated header mid-file, trailing all-zero filler rows, a duplicate part
number, a blank MRP cell, a literal `0` MRP, a lowercase part number and a
brand present in Inventory but with no MRP list. Add `--big 250000` for a
large inventory plus a matching MRP list and stock export.

### Check everything still works

```bash
python self_test.py
```

151 assertions covering every rule below, driven through the real HTTP endpoints.

---

## Inputs

| Input | Required | Expected columns |
|---|---|---|
| Inventory master | yes | `part_no`, `brand`, `part_name`, `mrp` |
| Brand MRP list (one per brand, add as many as you like) | at least one | anything — you map the columns in the UI |
| Warehouse stock | no | `part_no`, `available_quantity`, `brand` |

Column names are matched case- and punctuation-insensitively, so `Part_No`,
`PART NO` and `partno` all resolve to `part_no`.

Each file can be **uploaded** in the browser or **pointed at by path** on disk
(handy for a 50 MB inventory you don't want to copy around).

### MRP lists: no code changes per brand

Supplier sheets vary wildly, so nothing about their layout is hard-coded. After
a file is read, the UI shows dropdowns for that file's own headers:

- **Brand** the list is for (free text, auto-completed from the brands in Inventory)
- **Sheet** (XLSX only)
- **Header row** — auto-detected, so a sheet whose real header sits under three
  lines of company letterhead works out of the box; override it if the guess is wrong
- **Part number / Description / MRP columns** — auto-guessed from the header
  names, with a live five-row preview of the mapping

---

## Step 1 — Inventory vs MRP list

Part numbers are trimmed and uppercased before matching, and matching happens
**only within the same brand**. Brands in Inventory with no MRP list uploaded
are reported and skipped, never guessed at.

Each MRP list is cleaned first. Rows are dropped when they have no usable part
number, when they merely repeat the header text, or when every mapped cell is
blank or a literal `0`. Duplicate part numbers inside one list keep the **first**
occurrence, and the number dropped is reported in the UI.

Every part then lands in exactly one bucket:

| `issue_type` | Meaning |
|---|---|
| `MRP_MISMATCH` | In both files, MRPs differ, official MRP is not zero. `mrp_direction` is `INCREASE` or `DECREASE`. |
| `MISSING_IN_MRP_LIST` | In Inventory, not on the new list — possibly discontinued. |
| `MISSING_IN_INVENTORY` | On the new list, not set up in Inventory. |

Two kinds of part produce no row in Step 1:

- **Prices that already agree** — counted in the summary as "matched, MRP identical".
- **Parts the supplier prices at `0` or blank** (`MRP_LIST_SHOWS_ZERO`). These are
  bulk and non-retail packs — 210 L drums, 180 kg barrels — which have no printed
  MRP at all. They cannot be mispriced against an MRP they do not have, and they
  recur unchanged on every run, so they are kept out of the issue reports. Every
  one of them is in the **No MRP on list** tab (`list6_existing_part_no_mrp.csv`),
  and the summary states how many were set aside. They are never reported as a
  price cut to zero.

Output: **`step1_inventory_vs_mrp_issues.csv`** —
`brand, part_no, part_name, issue_type, mrp_inventory, mrp_official, mrp_direction, note`

### The same run, split into per-brand lists (the reconciliation workflow)

The **Lists per brand** section mirrors the actual reconciliation workflow —
each list is a specific action, not just an issue type. Every part touched by
a run lands in exactly one of these seven lists.

| List | File | Action | Columns |
|---|---|---|---|
| **Same MRP** | `list1_same_mrp.csv` | No change required. | `brand, part_no, part_name, mrp, available_quantity` |
| **MRP Increase** | `list2_mrp_increase.csv` | Update the dealer portal directly — no confirmation needed. | `brand, part_no, part_name, mrp_old, mrp_new, mrp_difference, mrp_change_pct, available_quantity` |
| **MRP Decrease** | `list3_mrp_decrease.csv` | Review before applying the cut. | same columns as Increase |
| **New Parts** | `list4_new_parts.csv` | Description reformatted; send for confirmation, then upload to the dealer portal. | `brand, part_no, part_name, mrp_new, available_quantity, status` |
| **New, no MRP** | `list5_ignored_new_parts_no_mrp.csv` | Ignored — no price to create the part with. | `brand, part_no, part_name, note` |
| **No MRP on list** | `list6_existing_part_no_mrp.csv` | Keep the current MRP as it is. | `brand, part_no, part_name, mrp_inventory, available_quantity, note` |
| **Not on list** | `list7_not_on_supplier_list.csv` | Keep the current MRP as it is. | `brand, part_no, part_name, mrp_inventory, available_quantity, note` |

`mrp_old` is the Inventory price, `mrp_new` the supplier's; `mrp_difference` and
`mrp_change_pct` are signed, so a cut reads `-41.50 / -12.2%`. `available_quantity`
comes from the Stock file wherever one was uploaded — it is shown for reference
on every list, never used to filter or drop a row here (that is what Step 2 is
for).

A few rules worth knowing:

- **A new part with no usable MRP (0 or blank) is ignored, not listed as a new
  part** — there is no price to set up a part master with. It still shows up in
  its own **New, no MRP** list so nothing vanishes silently; it simply never
  reaches the New Parts list you'd send for confirmation.
- **New Parts descriptions are reformatted** (`mrp_core.format_part_description`):
  whitespace collapsed, stray punctuation trimmed, title-cased with small words
  (`with`, `and`, `of`, …) kept lowercase — e.g. `"FILTER ELEMENT WITH O RING"`
  becomes `"Filter Element with O Ring"`. No specific company template was given,
  so this is a generic clean-up; if there's a house format to match, this is the
  one function to change. Only the New Parts list is reformatted — every other
  list keeps the source text as-is.
- **"No MRP on list" and "Not on list" both mean "keep the MRP as it is"** — the
  first is a part that's on the supplier's sheet with a blank/zero price
  (usually a bulk or non-retail pack), the second is a part the sheet doesn't
  mention at all (possibly discontinued). They're kept as two separate lists
  rather than merged, since the two situations call for different verification
  even though neither results in a price change.
- **MRP Increase and Decrease are never stock-filtered.** They are the full,
  raw comparison — every price change, regardless of stock level. That is a
  deliberate difference from the older Step 1/Step 2 reports below, which do
  filter by stock; see that section for why the two serve different purposes.

## Step 2 — filter against Stock

*(Step 1 and Step 2 are the original, detailed issue-tracking reports; the
seven-list workflow above is the newer, action-oriented view of the same data.
Both run on every check and stay independent — nothing here changes what the
lists above contain, or vice versa.)*

Only `MRP_MISMATCH` rows are filtered. Every other issue type passes through
unchanged, with `decision_reason` saying so — they have no old/new MRP pair to
weigh against stock.

Quantities are summed across rows, so a part held in several warehouses is
counted once, correctly.

| In stock? | Direction | Result |
|---|---|---|
| No | INCREASE | **Dropped** — no physical stock is mispriced |
| No | DECREASE | Kept |
| Yes | either | Kept — physical stock needs re-pricing/re-labelling |

If the stock file is missing, empty or 0 bytes, every part is treated as **not
in stock** (so only DECREASE mismatches survive) and the UI shows a banner:
*"No stock data was available — all hikes were assumed safe to ignore. Re-run
once a real stock export is available."* It never proceeds silently.

Output: **`step2_final_issues_after_stock_check.csv`** — the Step 1 columns plus
`available_quantity, in_stock, stock_data_available, decision_reason`.

---

## Notes on edge cases

- **Prices** like `9,250.00`, `₹ 1 200`, `(50)` and blanks are parsed rather than
  rejected. Comparison uses a half-paisa tolerance, so float noise never fires.
- **A blank MRP cell** on the official list is bucketed with `MRP_LIST_SHOWS_ZERO`
  (its note says the cell was blank rather than `0`). It is the same real-world
  situation — no printed MRP — and folding it there keeps the "exactly one
  bucket per part" rule while still surfacing the part for manual checking,
  instead of dropping it or reporting a price cut to zero.
- **A blank Inventory MRP** is compared as `0`, and the note says so.
- **Duplicate `brand`+`part_no` rows in Inventory** keep the first and are counted.
- **A stock file with no `brand` column** still works; stock is then matched on
  part number alone, and the UI warns that this can merge identical part numbers
  across brands.
- **Delimiter and encoding** are sniffed per file (`,` `;` tab `|`, UTF-8 or CP1252).
- **Two MRP lists for the same brand** are combined; overlapping part numbers
  keep the first list's price and the overlap count is reported.

## If a list matches nothing

Matching is per brand and per part number, so a list that comes back as 100%
"new parts" almost always means one of four things. The app now checks for all
of them rather than producing a confident, wrong answer:

- **The block still holds the previous file.** Picking a new file in an MRP
  block now clears that block completely - name, sheet, header row, columns and
  preview - before reading the new one. Run check is disabled while a list is
  still being read, so it can never run against the file you just replaced.
- **The wrong sheet.** If the chosen columns do not exist on the sheet and
  header row being read, the run is refused up front with a message naming the
  brand, the file, the sheet, the headers actually found there and the other
  sheets in the workbook.
- **The brand name does not match Inventory.** Typing a brand that is not in
  the Inventory file warns before the run.
- **The right brand on the wrong file.** After the run, if none of a list's
  part numbers were found under that brand in Inventory, a red banner says so
  with the counts, instead of quietly reporting tens of thousands of new parts.

## Where uploaded files live

An uploaded file is copied to a temp folder that is **unique to the running
process** (`%TEMP%/mrp_recon_uploads/run_<pid>/`) and deleted when that process
exits. Folders left behind by a killed run are pruned after seven days.

That isolation matters: with a single shared folder, stopping any second copy of
the app deleted the files belonging to the copy you were using, and every later
upload failed. Now one instance can never disturb another, the folder is
recreated if something removes it mid-session, and a file that has genuinely
gone produces a plain message rather than a server error.

The registry of uploads lives in memory, so restarting the app clears it. The
page notices (every response carries the server's id), clears your selections
and says so, instead of failing later with a confusing error.

**To avoid the copy entirely**, use *"or point at a file on disk"* and paste the
path. The file is then read where it sits - nothing is copied to temp, and a
50 MB inventory does not travel through the browser at all.

## Scale

Measured on a 52 MB / 750,000-row inventory with a 16 MB MRP list, producing
250,000 issue rows (three times the stated 250k-row target):

| Stage | Time |
|---|---|
| Parse inventory (chunked, 100k rows at a time) | 5.9 s |
| Run steps 1 + 2 | 8.3 s |
| Any results page (sorted, filtered) | < 0.25 s |
| Download the 43 MB step 2 CSV | 2.1 s |

Peak process memory ~690 MB. The browser never receives more than one page of
rows: filtering, sorting and paging all happen server-side, so the DOM holds
50 rows, not 250,000.

## Files

| File | What it is |
|---|---|
| `app.py` | Flask server, upload/run/results/download endpoints, entry point |
| `mrp_core.py` | All data logic: parsing, cleaning, Step 1, Step 2 |
| `static/index.html`, `app.css`, `app.js` | The single-page UI, no build step |
| `make_sample_data.py` | Generates realistic messy sample inputs |
| `self_test.py` | End-to-end assertions over the real endpoints |
