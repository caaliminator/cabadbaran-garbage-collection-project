/* ==========================================================================
   tables.js -- declarative client-side search, filter, and sort
   ==========================================================================

   Wire a table up entirely from markup -- no per-page JavaScript:

     <div data-table-controller="propTable">
       <input data-table-search placeholder="Search">
       <select data-table-filter="status">...</select>
       <span data-table-count></span>
       <table id="propTable">
         <thead>
           <tr><th data-sort-key="name" aria-sort="none">Name <svg class="icon"/></th></tr>
         </thead>
         <tbody>
           <tr data-status="Collected" data-name="Don Nica">...</tr>
         </tbody>
       </table>
       <div data-table-empty hidden>No matches</div>
     </div>

   - Search scans the row's visible text.
   - Each <select data-table-filter="x"> matches the row's data-x attribute.
     An empty value means "no filter".
   - Sorting uses data-<key> when present, otherwise the cell's text, and
     compares numerically when both values parse as numbers.
   ========================================================================== */

(function () {
  'use strict';

  const $  = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  class DataTable {
    constructor(scope) {
      this.scope  = scope;
      this.table  = document.getElementById(scope.dataset.tableController);
      if (!this.table) return;

      // Defaults to table rows, but any repeated element works -- the
      // collector history screens filter a list of record cards.
      const rowSelector = scope.dataset.tableRowSelector || 'tbody > tr';
      this.tbody   = $('tbody', this.table) || this.table;
      this.rows    = $$(rowSelector, this.table);
      this.search  = $('[data-table-search]', scope);
      this.filters = $$('[data-table-filter]', scope);
      this.count   = $('[data-table-count]', scope);
      this.empty   = $('[data-table-empty]', scope);
      this.headers = $$('th[data-sort-key]', this.table);

      this.bind();
      this.apply();
    }

    bind() {
      if (this.search) {
        this.search.addEventListener('input', debounce(() => this.apply(), 140));
        // Escape clears the query, matching platform search conventions.
        this.search.addEventListener('keydown', (e) => {
          if (e.key === 'Escape' && this.search.value) {
            this.search.value = '';
            this.apply();
          }
        });
      }

      this.filters.forEach((sel) =>
        sel.addEventListener('change', () => this.apply()));

      this.headers.forEach((th) => {
        th.setAttribute('tabindex', '0');
        th.setAttribute('role', 'columnheader');
        if (!th.hasAttribute('aria-sort')) th.setAttribute('aria-sort', 'none');

        const activate = () => this.sort(th);
        th.addEventListener('click', activate);
        th.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
        });
      });

      const reset = $('[data-table-reset]', this.scope);
      if (reset) {
        reset.addEventListener('click', () => {
          if (this.search) this.search.value = '';
          this.filters.forEach((f) => (f.value = ''));
          this.apply();
        });
      }
    }

    /* ---- Filtering ---- */

    apply() {
      const query = (this.search?.value || '').trim().toLowerCase();
      const active = this.filters
        .map((sel) => ({ key: sel.dataset.tableFilter, value: sel.value }))
        .filter((f) => f.value);

      let visible = 0;

      this.rows.forEach((row) => {
        const matchesQuery = !query ||
          (row.textContent || '').toLowerCase().includes(query);

        const matchesFilters = active.every(
          (f) => (row.dataset[f.key] || '') === f.value);

        const show = matchesQuery && matchesFilters;
        row.hidden = !show;
        if (show) visible++;
      });

      if (this.count) {
        const total = this.rows.length;
        this.count.textContent = visible === total
          ? `${total} ${total === 1 ? 'record' : 'records'}`
          : `${visible} of ${total} records`;
      }

      if (this.empty) this.empty.hidden = visible !== 0;
      // Hide the table itself when nothing matches so headers don't float alone.
      if (this.empty) this.table.hidden = visible === 0;
    }

    /* ---- Sorting ---- */

    sort(th) {
      const key = th.dataset.sortKey;
      const dir = th.getAttribute('aria-sort') === 'ascending'
        ? 'descending' : 'ascending';

      this.headers.forEach((h) =>
        h.setAttribute('aria-sort', h === th ? dir : 'none'));

      const factor = dir === 'ascending' ? 1 : -1;
      const index  = this.headers.includes(th) ? cellIndex(th) : -1;

      const sorted = [...this.rows].sort((a, b) => {
        const av = value(a, key, index);
        const bv = value(b, key, index);
        const an = parseFloat(String(av).replace(/[^0-9.\-]/g, ''));
        const bn = parseFloat(String(bv).replace(/[^0-9.\-]/g, ''));

        const numeric = !Number.isNaN(an) && !Number.isNaN(bn)
          && String(av).trim() !== '' && String(bv).trim() !== '';

        if (numeric) return (an - bn) * factor;
        return String(av).localeCompare(String(bv), undefined,
          { sensitivity: 'base', numeric: true }) * factor;
      });

      // One reflow instead of N.
      const frag = document.createDocumentFragment();
      sorted.forEach((row) => frag.appendChild(row));
      this.tbody.appendChild(frag);
      this.rows = sorted;
    }
  }

  /* ---- helpers ---- */

  function value(row, key, index) {
    if (key && row.dataset[key] !== undefined) return row.dataset[key];
    const cell = index >= 0 ? row.cells[index] : null;
    return cell ? cell.textContent.trim() : '';
  }

  function cellIndex(th) {
    return Array.from(th.parentElement.children).indexOf(th);
  }

  function debounce(fn, wait) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  }

  function init() {
    $$('[data-table-controller]').forEach((scope) => new DataTable(scope));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.GCTS = Object.assign(window.GCTS || {}, { DataTable });
})();
