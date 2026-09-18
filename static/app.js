/* MRP Reconciliation - frontend.
   Vanilla JS, no build step, no external requests. Every fetch below is a
   same-origin relative URL served by the local Flask process. */

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  inventoryFileId: null,
  stockFileId: null,
  mrp: [],            // {uid, fileId, name, columns, sheets, headerRow, sheet}
  runId: null,
  brands: [],
  issueTypes: [],
  stockAvailable: false,
};

let mrpCounter = 0;

// ---------------------------------------------------------------- utilities

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids) node.append(kid);
  return node;
}

function fmtBytes(n) {
  if (!n && n !== 0) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

const nf = new Intl.NumberFormat();
const fmtN = (n) => nf.format(n || 0);

function setStatus(node, kind, text, warnings) {
  if (!node) return;
  // classList, never className: the MRP blocks find this element by its
  // .mrp-status hook class, and overwriting className threw that hook away -
  // after the first status write the block could never be reloaded again.
  node.classList.remove('ok', 'err', 'busy');
  node.classList.add('status');
  if (kind) node.classList.add(kind);
  node.textContent = text;
  if (warnings && warnings.length) {
    const ul = el('ul');
    warnings.forEach((w) => ul.append(el('li', { text: w })));
    node.append(ul);
  }
}

// The server stamps every response with the id it was started under. If that
// changes, the app was restarted and every file_id this page holds is dead.
let serverId = null;

function checkServerRestart(res) {
  const id = res.headers.get('X-MRP-Server');
  if (!id) return;
  if (serverId && id !== serverId) {
    serverId = id;
    handleServerRestart();
  }
  serverId = id;
}

function handleServerRestart() {
  state.inventoryFileId = null;
  state.stockFileId = null;
  state.runId = null;
  setStatus($('#inventory-status'), 'err', 'Re-select this file - the app was restarted.');
  setStatus($('#stock-status'), 'err', 'Re-select this file - the app was restarted.');
  state.mrp.forEach((m) => {
    resetMrpEntry(m, 'MRP list');
    setStatus($('.mrp-status', m.node), 'err', 'Re-select this file - the app was restarted.');
  });
  ['#card-summary', '#card-lists', '#card-step1', '#card-step2']
    .forEach((id) => $(id).classList.add('hidden'));
  clearBanners();
  banner('err', 'The app was restarted and no longer has your files',
    ['Everything runs in memory, so a restart clears the uploads. Re-select the '
     + 'inventory, stock and MRP lists on this page, then run the check again.']);
  updateRunButton();
}

async function postForm(url, formData) {
  const res = await fetch(url, { method: 'POST', body: formData });
  checkServerRestart(res);
  const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
  if (!data.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  checkServerRestart(res);
  const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
  if (!data.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function getJSON(url) {
  const res = await fetch(url);
  checkServerRestart(res);
  const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
  if (!data.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function fillSelect(sel, options, selected, placeholder) {
  sel.textContent = '';
  if (placeholder !== undefined) sel.append(el('option', { value: '', text: placeholder }));
  options.forEach((o) => {
    const value = typeof o === 'object' ? String(o.value) : String(o);
    const label = typeof o === 'object' ? o.label : String(o);
    const opt = el('option', { value, text: label });
    if (selected !== undefined && selected !== null && String(selected) === value) {
      opt.selected = true;
    }
    sel.append(opt);
  });
}

function refreshBrandOptions() {
  const dl = $('#brand-options');
  dl.textContent = '';
  state.brands.forEach((b) => dl.append(el('option', { value: b })));
}

function updateRunButton() {
  const busy = state.mrp.some((m) => m.busy);
  const broken = state.mrp.some((m) => m.error);
  const ready = Boolean(state.inventoryFileId) && state.mrp.some((m) => m.fileId)
                && !busy && !broken;
  $('#run-btn').disabled = !ready;
  const note = $('#run-note');
  if (!state.inventoryFileId) note.textContent = 'Add the inventory master to continue.';
  else if (busy) note.textContent = 'Still reading an MRP list...';
  else if (broken) note.textContent = 'One MRP list did not read cleanly - fix it first.';
  else if (!state.mrp.some((m) => m.fileId)) note.textContent = 'Add at least one brand MRP list.';
  else if (!state.stockFileId) note.textContent = 'No stock file - all parts will count as out of stock.';
  else note.textContent = '';
}

// ------------------------------------------------- inventory & stock slots

async function sendSimple(kind, statusNode, fileInput, pathInput) {
  const fd = new FormData();
  if (fileInput && fileInput.files && fileInput.files[0]) {
    fd.append('file', fileInput.files[0]);
  } else if (pathInput && pathInput.value.trim()) {
    fd.append('path', pathInput.value.trim());
  } else {
    setStatus(statusNode, 'err', 'Choose a file or enter a path first.');
    return null;
  }
  setStatus(statusNode, 'busy', 'Reading file...');
  try {
    const data = await postForm(`/api/${kind}`, fd);
    return data;
  } catch (e) {
    setStatus(statusNode, 'err', e.message);
    return null;
  }
}

async function handleInventory() {
  const node = $('#inventory-status');
  const data = await sendSimple('inventory', node, $('#inventory-file'), $('#inventory-path'));
  if (!data) { state.inventoryFileId = null; updateRunButton(); return; }
  state.inventoryFileId = data.file_id;
  state.brands = data.brands || [];
  refreshBrandOptions();
  setStatus(node, 'ok',
    `OK - ${data.name} (${fmtBytes(data.size)}): ${fmtN(data.rows_read)} rows read, ` +
    `${fmtN(data.unique_parts)} unique brand+part rows, ${state.brands.length} brand(s).`,
    data.warnings);
  // Re-offer brands to any MRP block already on screen.
  state.mrp.forEach((m) => { if (m.node) refreshBrandOptions(); });
  updateRunButton();
}

async function handleStock() {
  const node = $('#stock-status');
  const data = await sendSimple('stock', node, $('#stock-file'), $('#stock-path'));
  if (!data) { state.stockFileId = null; updateRunButton(); return; }
  state.stockFileId = data.file_id;
  state.stockAvailable = data.available;
  if (data.available) {
    setStatus(node, 'ok',
      `OK - ${data.name}: ${fmtN(data.rows_read)} rows, ${fmtN(data.unique_parts)} ` +
      `unique part(s) after summing across warehouses.`, data.warnings);
  } else {
    setStatus(node, 'err',
      `${data.name} holds no usable stock rows - the run will treat every part as ` +
      `out of stock.`, data.warnings);
  }
  updateRunButton();
}

// ------------------------------------------------------------- MRP blocks

function addMrpBlock() {
  const uid = `mrp${++mrpCounter}`;
  const frag = $('#mrp-template').content.cloneNode(true);
  const node = frag.querySelector('.mrp-item');
  node.dataset.uid = uid;
  $('.mrp-title', node).textContent = `MRP list #${mrpCounter}`;

  const entry = { uid, node, fileId: null, name: null, columns: [], sheets: [],
                  headerRow: 0, sheet: null, preview: [] };
  state.mrp.push(entry);

  $('.remove', node).addEventListener('click', () => {
    state.mrp = state.mrp.filter((m) => m.uid !== uid);
    node.remove();
    $('#mrp-empty').classList.toggle('hidden', state.mrp.length > 0);
    updateRunButton();
  });

  $('.mrp-file', node).addEventListener('change', () => loadMrp(entry, 'file'));
  $('.mrp-path-go', node).addEventListener('click', () => loadMrp(entry, 'path'));

  $('.map-header', node).addEventListener('change', () => resniff(entry));
  $('.map-sheet', node).addEventListener('change', () => resniff(entry, true));
  ['.map-part', '.map-desc', '.map-mrp'].forEach((s) =>
    $(s, node).addEventListener('change', () => renderPreview(entry)));
  $('.map-brand', node).addEventListener('input', updateRunButton);

  $('#mrp-list').append(frag);
  $('#mrp-empty').classList.add('hidden');
  refreshBrandOptions();
  updateRunButton();
  return entry;
}

function resetMrpEntry(entry, title) {
  // Wipe everything the previous file left behind. Without this a block can
  // show one file's columns while still pointing at another file, and the run
  // silently reads the wrong sheet.
  entry.fileId = null;
  entry.name = null;
  entry.columns = [];
  entry.sheets = [];
  entry.preview = [];
  entry.sheet = null;
  entry.headerRow = 0;
  entry.error = false;
  const node = entry.node;
  $('.mapping', node).classList.add('hidden');
  ['.map-part', '.map-desc', '.map-mrp', '.map-header', '.map-sheet']
    .forEach((sel) => { $(sel, node).textContent = ''; });
  $('.mrp-title', node).textContent = title || 'MRP list';
  updateRunButton();
}

async function loadMrp(entry, source) {
  const node = entry.node;
  const status = $('.mrp-status', node);
  const fd = new FormData();
  if (source === 'file') {
    const f = $('.mrp-file', node).files[0];
    if (!f) return;
    fd.append('file', f);
  } else {
    const p = $('.mrp-path', node).value.trim();
    if (!p) { setStatus(status, 'err', 'Enter a path first.'); return; }
    fd.append('path', p);
  }
  const label = source === 'file' ? $('.mrp-file', node).files[0].name
                                  : $('.mrp-path', node).value.trim();
  resetMrpEntry(entry, `MRP list - ${label}`);
  entry.busy = true;
  updateRunButton();
  setStatus(status, 'busy', `Reading headers from ${label}...`);
  try {
    const data = await postForm('/api/mrp', fd);
    entry.fileId = data.file_id;
    entry.name = data.name;
    applySniff(entry, data);
    setStatus(status, 'ok',
      `OK - ${data.name} (${fmtBytes(data.size)}): ${data.columns.length} column(s) ` +
      `on row ${data.header_row + 1}` +
      (data.sheet ? `, sheet '${data.sheet}'` : '') + '. Check the mapping below.');
    $('.mrp-title', node).textContent = `MRP list - ${data.name}`;
  } catch (e) {
    resetMrpEntry(entry, 'MRP list');
    setStatus(status, 'err', e.message);
  }
  entry.busy = false;
  updateRunButton();
}

async function resniff(entry, sheetChanged) {
  if (!entry.fileId) return;
  const node = entry.node;
  const headerRow = sheetChanged ? null : parseInt($('.map-header', node).value, 10);
  const sheet = $('.map-sheet', node).value || null;
  const status = $('.mrp-status', node);
  entry.busy = true;
  updateRunButton();
  setStatus(status, 'busy', 'Re-reading headers...');
  try {
    const data = await postJSON('/api/mrp/resniff', {
      file_id: entry.fileId, header_row: headerRow, sheet,
    });
    applySniff(entry, data);
    entry.error = false;
    setStatus(status, 'ok',
      `OK - ${entry.name}: ${data.columns.length} column(s) on row ${data.header_row + 1}`
      + (data.sheet ? `, sheet '${data.sheet}'.` : '.'));
  } catch (e) {
    // The columns on screen no longer describe the chosen sheet, so the block
    // is unusable until the user picks something that reads cleanly.
    entry.error = true;
    $('.mapping', node).classList.add('hidden');
    setStatus(status, 'err', `${e.message} This list cannot be used until it reads cleanly.`);
  }
  entry.busy = false;
  updateRunButton();
}

function applySniff(entry, data) {
  const node = entry.node;
  entry.columns = data.columns || [];
  entry.sheets = data.sheets || [];
  entry.headerRow = data.header_row;
  entry.sheet = data.sheet;
  entry.preview = data.preview || [];

  $('.mapping', node).classList.remove('hidden');

  const sheetLabel = $('.sheet-label', node);
  if (entry.sheets.length) {
    sheetLabel.classList.remove('hidden');
    fillSelect($('.map-sheet', node), entry.sheets, entry.sheet || entry.sheets[0]);
  } else {
    sheetLabel.classList.add('hidden');
  }

  fillSelect($('.map-header', node),
    (data.header_row_choices || []).map((c) => ({
      value: c.index, label: `Row ${c.index + 1}: ${c.text}`,
    })), data.header_row);

  const guess = data.guess || {};
  fillSelect($('.map-part', node), entry.columns, guess.part_col, '-- pick a column --');
  fillSelect($('.map-desc', node), entry.columns, guess.desc_col, '(none)');
  fillSelect($('.map-mrp', node), entry.columns, guess.mrp_col, '-- pick a column --');

  // Offer a brand guess from the file name if the user hasn't typed one.
  const brandInput = $('.map-brand', node);
  if (!brandInput.value && entry.name) {
    const stem = entry.name.replace(/\.[^.]+$/, '').toUpperCase();
    const hit = state.brands.find((b) => stem.includes(b.toUpperCase()));
    if (hit) brandInput.value = hit;
  }
  renderPreview(entry);
}

function renderPreview(entry) {
  const node = entry.node;
  const table = $('table.preview', node);
  const cols = entry.columns;
  const pick = {
    part: $('.map-part', node).value,
    desc: $('.map-desc', node).value,
    mrp: $('.map-mrp', node).value,
  };
  const idx = (name) => cols.indexOf(name);
  const head = $('thead', table);
  const body = $('tbody', table);
  head.textContent = '';
  body.textContent = '';
  const hr = el('tr');
  ['part_no', 'description', 'mrp'].forEach((h) => hr.append(el('th', { text: h })));
  head.append(hr);
  entry.preview.slice(0, 5).forEach((row) => {
    const tr = el('tr');
    [pick.part, pick.desc, pick.mrp].forEach((colName) => {
      const i = colName ? idx(colName) : -1;
      tr.append(el('td', { text: i >= 0 && row[i] !== undefined ? row[i] : '' }));
    });
    body.append(tr);
  });
  updateRunButton();
}

function validateMrpBlocks() {
  // Guard against a block whose dropdowns no longer describe the file it holds.
  const problems = [];
  state.mrp.filter((m) => m.fileId).forEach((m) => {
    const who = `"${$('.map-brand', m.node).value.trim() || 'unnamed brand'}" (${m.name})`;
    const picked = {
      'part-number': $('.map-part', m.node).value,
      description: $('.map-desc', m.node).value,
      MRP: $('.map-mrp', m.node).value,
    };
    Object.entries(picked).forEach(([label, col]) => {
      if (col && !m.columns.includes(col)) {
        problems.push(`${who}: the ${label} column "${col}" is not in this file - re-pick it.`);
      }
    });
    if (picked['part-number'] && picked['part-number'] === picked.MRP) {
      problems.push(`${who}: "${picked.MRP}" is selected as both the part-number column `
        + `and the MRP column. Pick the column that holds the part numbers.`);
    }
    if (picked['part-number'] && picked['part-number'] === picked.description) {
      problems.push(`${who}: "${picked.description}" is selected as both the part-number `
        + `column and the description column. Pick the column that holds the part numbers.`);
    }
    const sheetSel = $('.map-sheet', m.node);
    if (m.sheets.length && sheetSel.value && m.sheet && sheetSel.value !== m.sheet) {
      problems.push(
        `${who}: sheet "${sheetSel.value}" is selected but the columns shown were read `
        + `from "${m.sheet}". Re-pick the sheet.`);
    }
  });
  return problems;
}

function collectMrpSpecs() {
  return state.mrp.filter((m) => m.fileId).map((m) => ({
    file_id: m.fileId,
    brand: $('.map-brand', m.node).value.trim(),
    part_col: $('.map-part', m.node).value || null,
    desc_col: $('.map-desc', m.node).value || null,
    mrp_col: $('.map-mrp', m.node).value || null,
    header_row: parseInt($('.map-header', m.node).value, 10) || 0,
    sheet: $('.map-sheet', m.node).value || null,
  }));
}

// ------------------------------------------------------------------- banners

function clearBanners() { $('#banners').textContent = ''; }

function banner(kind, title, lines) {
  const box = el('div', { class: `banner ${kind}` }, el('strong', { text: title }));
  if (lines && lines.length) {
    const ul = el('ul');
    lines.forEach((l) => ul.append(el('li', { text: l })));
    box.append(ul);
  }
  $('#banners').append(box);
}

// ----------------------------------------------------------------- the run

async function runCheck() {
  clearBanners();
  const specs = collectMrpSpecs();
  const missing = specs.filter((s) => !s.brand || !s.part_col || !s.mrp_col);
  if (missing.length) {
    banner('err', 'Finish the column mapping first',
      ['Every MRP list needs a brand, a part-number column and an MRP column.']);
    return;
  }
  const problems = validateMrpBlocks();
  if (problems.length) {
    banner('err', 'An MRP list does not match its column mapping', problems);
    return;
  }
  const unknown = specs.map((s) => s.brand).filter((b) =>
    state.brands.length && !state.brands.some((x) => x.toUpperCase() === b.toUpperCase()));
  if (unknown.length) {
    banner('warn', 'A brand you typed is not in the Inventory file',
      [`${[...new Set(unknown)].join(', ')} - Inventory uses: ${state.brands.join(', ')}. `
       + `Matching is per brand, so a brand that is spelt differently will match nothing.`]);
  }
  const dupBrands = specs.map((s) => s.brand.toUpperCase())
    .filter((b, i, a) => a.indexOf(b) !== i);
  if (dupBrands.length) {
    banner('warn', 'The same brand is on more than one MRP list',
      [`${[...new Set(dupBrands)].join(', ')} - the lists will be combined, `
       + `and the first upload wins where a part number appears in both.`]);
  }
  $('#run-btn').disabled = true;
  $('#run-spinner').classList.remove('hidden');
  $('#run-note').textContent = 'Running step 1 and step 2...';
  try {
    const data = await postJSON('/api/run', {
      inventory_file_id: state.inventoryFileId,
      stock_file_id: state.stockFileId,
      mrp_lists: specs,
    });
    state.runId = data.run_id;
    state.brands = data.brands.length ? data.brands : state.brands;
    state.issueTypes = data.issue_types;
    state.stockAvailable = data.stock_data_available;
    refreshBrandOptions();
    renderSummary(data);
    if (!data.stock_data_available) banner('warn', data.stock_warning);
    if (data.warnings && data.warnings.length) {
      banner('warn', 'Data notes', data.warnings);
    }
    if (data.suspect_mapping && data.suspect_mapping.length) {
      banner('err', 'The part-number column looks wrong', data.suspect_mapping);
    }
    if (data.brand_mismatch && data.brand_mismatch.length) {
      banner('err', 'This MRP list does not look like it belongs to that brand',
        data.brand_mismatch.concat([
          'Check that the file you picked really is that brand’s price list, that the '
          + 'brand name matches the one used in Inventory, and that the part-number column '
          + '(and sheet) are the right ones.']));
    }
    if (data.skipped_brands && data.skipped_brands.length) {
      banner('warn', 'Brands in Inventory with no MRP list uploaded (not checked)',
        [data.skipped_brands.join(', ')]);
    }
    renderListTabs(data);
    tables.step1.reset(data.brands, data.issue_types);
    tables.step2.reset(data.brands, data.issue_types);
    $('#card-lists').classList.remove('hidden');
    $('#card-step1').classList.remove('hidden');
    $('#card-step2').classList.remove('hidden');
    await Promise.all([tables.lists.load(), tables.step1.load(), tables.step2.load()]);
    $('#run-note').textContent = 'Done.';
  } catch (e) {
    banner('err', 'The check could not be completed', [e.message]);
    $('#run-note').textContent = '';
  } finally {
    $('#run-spinner').classList.add('hidden');
    updateRunButton();
  }
}

function renderSummary(data) {
  const box = $('#summary-body');
  box.textContent = '';

  const chips = el('div', { class: 'chips' });
  const chip = (n, l) => {
    const c = el('div', { class: 'chip' });
    c.append(el('span', { class: 'n', text: fmtN(n) }));
    c.append(el('span', { class: 'l', text: l }));
    return c;
  };
  chips.append(chip(data.inventory.unique_parts, 'inventory parts'));
  chips.append(chip(data.matched_ok, 'matched, MRP identical'));
  chips.append(chip(data.step1.total, 'step 1 issues'));
  chips.append(chip(data.step2.dropped, 'dropped by stock filter'));
  chips.append(chip(data.step2.total, 'final issues (step 2)'));
  box.append(chips);

  // Per brand / issue type counts.
  const cols = data.issue_types;
  const mk = (stepKey, title) => {
    const wrap = el('div');
    wrap.append(el('div', { class: 'preview-title', text: title }));
    const t = el('table');
    const thead = el('thead');
    const hr = el('tr');
    hr.append(el('th', { class: 'nosort', text: 'Brand' }));
    cols.forEach((c) => hr.append(el('th', { class: 'nosort', text: c })));
    hr.append(el('th', { class: 'nosort', text: 'Total' }));
    thead.append(hr);
    t.append(thead);
    const tb = el('tbody');
    const byBrand = data[stepKey].by_brand || {};
    const brands = Object.keys(byBrand).sort();
    brands.forEach((b) => {
      const tr = el('tr');
      tr.append(el('td', { text: b }));
      let total = 0;
      cols.forEach((c) => {
        const n = byBrand[b][c] || 0;
        total += n;
        tr.append(el('td', { class: 'num', text: n ? fmtN(n) : '-' }));
      });
      tr.append(el('td', { class: 'num', text: fmtN(total) }));
      tb.append(tr);
    });
    const tr = el('tr');
    tr.append(el('th', { class: 'nosort', text: 'All brands' }));
    cols.forEach((c) => tr.append(el('th', {
      class: 'nosort num', text: fmtN(data[stepKey].totals[c] || 0),
    })));
    tr.append(el('th', { class: 'nosort num', text: fmtN(data[stepKey].total) }));
    tb.append(tr);
    t.append(tb);
    const sc = el('div', { class: 'scroll small' });
    sc.append(t);
    wrap.append(sc);
    return wrap;
  };

  const grid = el('div', { class: 'grid2' });
  grid.append(mk('step1', 'Step 1 - issues per brand'));
  grid.append(mk('step2', 'Step 2 - final issues per brand (after stock filter)'));
  box.append(grid);

  const files = el('p', { class: 'hint' });
  const bits = data.mrp_files.map((f) => {
    const d = f.dropped || {};
    const junk = (d.no_part_no || 0) + (d.header_repeat || 0) + (d.all_blank_or_zero || 0);
    return `${f.brand}: ${f.name} - ${fmtN(f.usable_rows)} usable rows ` +
           `(${fmtN(junk)} junk rows stripped, ${fmtN(d.duplicates || 0)} duplicates dropped)`;
  });
  files.textContent = `Inventory: ${data.inventory.name} - ${fmtN(data.inventory.rows_read)} rows read. ` +
    (data.stock_data_available
      ? `Stock: ${fmtN(data.stock.rows_read)} rows, ${fmtN(data.stock.unique_parts)} parts. `
      : 'Stock: none. ') + bits.join(' | ');
  box.append(files);

  if (data.no_mrp_on_list) {
    box.append(el('p', { class: 'hint',
      text: `${fmtN(data.no_mrp_on_list)} part(s) carry no MRP on the supplier list `
        + `(0 or blank - bulk/non-retail packs). They are not price issues, so they are `
        + `kept out of Step 1 and Step 2; see the "No MRP on list" tab above for all of them.` }));
  }
  if (data.step2.dropped) {
    box.append(el('p', { class: 'hint',
      text: `${fmtN(data.step2.dropped)} MRP_MISMATCH row(s) dropped in step 2: ${data.step2.dropped_reason}.` }));
  }
  $('#card-summary').classList.remove('hidden');
}

// --------------------------------------------------------- per-brand lists

function renderListTabs(data) {
  const strip = $('#list-tabs');
  strip.textContent = '';
  const order = data.list_order || [];
  const first = order.find((k) => data.lists[k].total > 0) || order[0];

  const select = (key) => {
    $$('.tab', strip).forEach((b) => b.classList.toggle('active', b.dataset.key === key));
    $('#list-blurb').textContent = data.lists[key].blurb
      + `  Downloads as ${data.lists[key].file}.`;
    tables.lists.setStep(key);
    tables.lists.reset(data.brands, data.issue_types);
    tables.lists.load();
  };

  order.forEach((key) => {
    const info = data.lists[key];
    const btn = el('button', { type: 'button', class: 'tab', 'data-key': key });
    btn.append(el('span', { class: 'tab-n', text: fmtN(info.total) }));
    btn.append(el('span', { class: 'tab-l', text: info.label }));
    btn.addEventListener('click', () => select(key));
    strip.append(btn);
  });
  select(first);
}

// ------------------------------------------------------------ result tables

const NUM_COLS = new Set(['mrp_inventory', 'mrp_official', 'available_quantity',
                          'mrp', 'mrp_old', 'mrp_new', 'mrp_difference', 'mrp_change_pct']);
const PILL_COLS = new Set(['issue_type', 'mrp_direction', 'in_stock', 'stock_data_available']);
const HEAD_LABEL = {
  brand: 'Brand', part_no: 'Part no', part_name: 'Part name', issue_type: 'Issue type',
  mrp_inventory: 'MRP (inventory)', mrp_official: 'MRP (official)',
  mrp_direction: 'Direction', note: 'Note', available_quantity: 'Qty available',
  in_stock: 'In stock', stock_data_available: 'Stock data?', decision_reason: 'Decision reason',
  mrp: 'MRP', mrp_old: 'MRP old (inventory)', mrp_new: 'MRP new (official)',
  mrp_difference: 'Difference', mrp_change_pct: 'Change %', status: 'Status',
};

function makeTable(step) {
  const root = $(`.tablewrap[data-step="${step}"]`);
  let stepKey = step;   // the "lists" widget swaps this as the user picks a tab
  const ui = {
    brand: $('[data-role=brand]', root),
    issue: $('[data-role=issue]', root),
    q: $('[data-role=q]', root),
    pagesize: $('[data-role=pagesize]', root),
    thead: $('thead', root),
    tbody: $('tbody', root),
    pageinfo: $('[data-role=pageinfo]', root),
  };
  let page = 1, sort = '', dir = 'asc', pages = 1, columns = [];

  function params() {
    const p = new URLSearchParams({
      page: String(page), page_size: ui.pagesize.value,
    });
    if (ui.brand.value) p.set('brand', ui.brand.value);
    if (ui.issue.value) p.set('issue_type', ui.issue.value);
    if (ui.q.value.trim()) p.set('q', ui.q.value.trim());
    if (sort) { p.set('sort', sort); p.set('dir', dir); }
    return p;
  }

  async function load() {
    if (!state.runId) return;
    const data = await getJSON(`/api/results/${state.runId}/${stepKey}?${params()}`);
    columns = data.columns;
    pages = data.pages;
    page = data.page;
    renderHead();
    renderBody(data.rows);
    const from = data.total ? (data.page - 1) * data.page_size + 1 : 0;
    const to = Math.min(data.page * data.page_size, data.total);
    ui.pageinfo.textContent =
      `${fmtN(from)}-${fmtN(to)} of ${fmtN(data.total)} row(s) - page ${data.page} / ${data.pages}`;
  }

  function renderHead() {
    ui.thead.textContent = '';
    const tr = el('tr');
    columns.forEach((c) => {
      const th = el('th', { text: HEAD_LABEL[c] || c, title: 'Click to sort' });
      if (sort === c) th.append(el('span', { class: 'arrow', text: dir === 'asc' ? '▲' : '▼' }));
      th.addEventListener('click', () => {
        if (sort === c) dir = dir === 'asc' ? 'desc' : 'asc';
        else { sort = c; dir = 'asc'; }
        page = 1;
        load();
      });
      tr.append(th);
    });
    ui.thead.append(tr);
  }

  function renderBody(rows) {
    ui.tbody.textContent = '';
    if (!rows.length) {
      const tr = el('tr');
      tr.append(el('td', { colspan: String(columns.length || 1), class: 'empty-rows',
                           text: 'No rows match the current filters.' }));
      ui.tbody.append(tr);
      return;
    }
    const frag = document.createDocumentFragment();
    rows.forEach((row) => {
      const tr = el('tr');
      columns.forEach((c) => {
        const v = row[c] || '';
        if (PILL_COLS.has(c) && v) {
          const td = el('td');
          td.append(el('span', { class: `pill ${v}`, text: v }));
          tr.append(td);
        } else if (NUM_COLS.has(c)) {
          tr.append(el('td', { class: 'num', text: v }));
        } else if (c === 'note' || c === 'decision_reason') {
          tr.append(el('td', { class: 'note', text: v, title: v }));
        } else if (c === 'part_name') {
          tr.append(el('td', { class: 'wrap', text: v, title: v }));
        } else {
          tr.append(el('td', { text: v }));
        }
      });
      frag.append(tr);
    });
    ui.tbody.append(frag);
  }

  function download(filtered) {
    if (!state.runId) return;
    const p = new URLSearchParams();
    if (filtered) {
      p.set('filtered', '1');
      if (ui.brand.value) p.set('brand', ui.brand.value);
      if (ui.issue.value) p.set('issue_type', ui.issue.value);
    }
    // A temporary <a download> rather than location.assign, so a browser that
    // decides to render the CSV instead of saving it can't navigate the app away.
    const a = el('a', { href: `/api/download/${state.runId}/${stepKey}?${p}`, download: '' });
    a.style.display = 'none';
    document.body.append(a);
    a.click();
    a.remove();
  }

  [ui.brand, ui.issue, ui.pagesize].forEach((n) =>
    n.addEventListener('change', () => { page = 1; load(); }));
  let qTimer;
  ui.q.addEventListener('input', () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { page = 1; load(); }, 250);
  });
  $('[data-role=first]', root).addEventListener('click', () => { page = 1; load(); });
  $('[data-role=prev]', root).addEventListener('click', () => { page = Math.max(1, page - 1); load(); });
  $('[data-role=next]', root).addEventListener('click', () => { page = Math.min(pages, page + 1); load(); });
  $('[data-role=last]', root).addEventListener('click', () => { page = pages; load(); });
  $('[data-role=download]', root).addEventListener('click', () => download(false));
  $('[data-role=download-filtered]', root).addEventListener('click', () => download(true));

  return {
    load,
    setStep(key) { stepKey = key; page = 1; sort = ''; dir = 'asc'; },
    reset(brands, issueTypes) {
      page = 1; sort = ''; dir = 'asc';
      fillSelect(ui.brand, brands, '', 'All brands');
      fillSelect(ui.issue, issueTypes, '', 'All issue types');
      ui.q.value = '';
    },
  };
}

const tables = {};

// ----------------------------------------------------------------- wiring

document.addEventListener('DOMContentLoaded', () => {
  tables.lists = makeTable('lists');
  tables.step1 = makeTable('step1');
  tables.step2 = makeTable('step2');

  $('#inventory-file').addEventListener('change', handleInventory);
  $('#inventory-path-go').addEventListener('click', handleInventory);
  $('#stock-file').addEventListener('change', handleStock);
  $('#stock-path-go').addEventListener('click', handleStock);
  $('#add-mrp').addEventListener('click', addMrpBlock);
  $('#run-btn').addEventListener('click', runCheck);

  $('#reset-btn').addEventListener('click', async () => {
    await fetch('/api/reset', { method: 'POST' }).catch(() => {});
    window.location.reload();
  });

  applyMode();
  addMrpBlock();
  updateRunButton();
});

async function applyMode() {
  // The header badge must not claim "offline" when the app is on a server.
  let mode;
  try {
    mode = await getJSON('/api/mode');
  } catch (e) {
    return;
  }
  if (!mode.hosted) return;
  const badge = $('.offline-badge');
  badge.textContent = 'hosted · files are uploaded to the server';
  badge.classList.add('hosted');
  badge.title = 'This copy runs on a remote server, so your files travel over the '
    + 'internet to it. Run it on your own PC for a fully offline check.';
  if (!mode.allow_local_paths) {
    $$('.pathbox').forEach((d) => d.classList.add('hidden'));
    $$('.mrp-path, .mrp-path-go').forEach((n) => n.classList.add('hidden'));
  }
}
