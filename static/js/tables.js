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
   - Paging: every table shows 10 rows a page by default, with a Show 5 / 10 /
     15 picker and Previous / Next. Paging runs over the rows the search and
     filters leave, so page 2 is page 2 of the matches, and any change to the
     search, a filter or the sort goes back to page 1. A table of 5 rows or
     fewer has nothing to page and shows no pager. The pager goes in the
     card's footer beside the row count, or in a footer made for it.
     Opt out with data-table-paged="false" on the controller.
   ========================================================================== */

(function () {
  'use strict';

  const $  = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  const PAGE_SIZES = [5, 10, 15];
  const DEFAULT_PAGE_SIZE = 10;

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

      this.paged    = scope.dataset.tablePaged !== 'false';
      this.pageSize = DEFAULT_PAGE_SIZE;
      this.page     = 1;
      if (this.paged) this.buildPager();

      this.bind();
      this.apply();
    }

    /* ---- Paging ---- */

    buildPager() {
      const pager = document.createElement('div');
      pager.className = 'table-pager';
      pager.hidden = true;
      pager.innerHTML =
        '<label class="table-pager__size">Show '
        + '<select class="field__select field__select--sm" data-pager-size>'
        + PAGE_SIZES.map((n) => `<option value="${n}"${n === DEFAULT_PAGE_SIZE ? ' selected' : ''}>${n}</option>`).join('')
        + '</select> per page</label>'
        + '<nav class="pager" aria-label="Table pages">'
        + '<button type="button" class="btn btn--secondary btn--sm" data-pager-prev>'
        + '<span aria-hidden="true">&lsaquo;</span> Previous</button>'
        + '<span class="table-pager__page" data-pager-page aria-live="polite"></span>'
        + '<button type="button" class="btn btn--secondary btn--sm" data-pager-next>'
        + 'Next <span aria-hidden="true">&rsaquo;</span></button>'
        + '</nav>';

      // Beside the row count when the card has a footer for it; otherwise in
      // a footer of its own, after everything else in the card.
      let foot = this.count ? this.count.closest('.card__foot') : null;
      if (!foot || !this.scope.contains(foot)) {
        foot = document.createElement('div');
        foot.className = 'card__foot';
        this.scope.appendChild(foot);
        this.madeFoot = foot;
      }
      foot.appendChild(pager);

      this.pager = pager;
      this.sizeSelect = $('[data-pager-size]', pager);
      this.prevBtn = $('[data-pager-prev]', pager);
      this.nextBtn = $('[data-pager-next]', pager);
      this.pageLabel = $('[data-pager-page]', pager);

      this.sizeSelect.addEventListener('change', () => {
        this.pageSize = Number(this.sizeSelect.value) || DEFAULT_PAGE_SIZE;
        this.page = 1;
        this.apply({ keepPage: true });
      });
      this.prevBtn.addEventListener('click', () => this.turn(-1));
      this.nextBtn.addEventListener('click', () => this.turn(1));
    }

    turn(step) {
      this.page += step;
      this.apply({ keepPage: true });
      // Keep the top of the table in view when the new page is shorter than
      // the one it replaced and the page would otherwise jump.
      const top = this.scope.getBoundingClientRect().top;
      if (top < 0) this.scope.scrollIntoView({ block: 'start', behavior: 'smooth' });
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

      // A filter inside the table's own scope that another script changes
      // says so with this event.
      this.scope.addEventListener('table:refresh', () => this.apply());

      // Tabs: <div data-table-tabs="group"> holding buttons with
      // data-table-tab="pending" / "collected" / ... / "all". A tab sets the hidden
      // <input data-table-filter="group">, "all" clears it. The chosen tab is
      // written to ?tab= so a refresh, or a link, opens on the same one.
      const tabs = $('[data-table-tabs]', this.scope);
      if (tabs) {
        const key = tabs.dataset.tableTabs;
        const input = this.filters.find((f) => f.dataset.tableFilter === key);
        $$('[data-table-tab]', tabs).forEach((btn) => {
          btn.addEventListener('click', () => {
            const tab = btn.dataset.tableTab;
            if (input) input.value = tab === 'all' ? '' : tab;
            $$('[data-table-tab]', tabs).forEach((b) =>
              b.setAttribute('aria-selected', b === btn ? 'true' : 'false'));
            this.apply();
            try {
              const url = new URL(window.location.href);
              url.searchParams.set('tab', tab);
              window.history.replaceState(null, '', url);
            } catch (err) { /* an old browser keeps working, just unbookmarked */ }
          });
        });
      }

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

    apply({ keepPage = false } = {}) {
      // A new search, filter or sort starts again from the first page: page 3
      // of the old matches means nothing once the matches have changed.
      if (!keepPage) this.page = 1;

      const query = (this.search?.value || '').trim().toLowerCase();
      const active = this.filters
        .map((sel) => ({ key: sel.dataset.tableFilter, value: sel.value }))
        .filter((f) => f.value);

      const matching = this.rows.filter((row) => {
        const matchesQuery = !query ||
          (row.textContent || '').toLowerCase().includes(query);
        const matchesFilters = active.every(
          (f) => (row.dataset[f.key] || '') === f.value);
        return matchesQuery && matchesFilters;
      });
      const visible = matching.length;

      // Which slice of the matches this page shows.
      const size = this.paged ? this.pageSize : Math.max(visible, 1);
      const pages = Math.max(1, Math.ceil(visible / size));
      this.page = Math.min(Math.max(1, this.page), pages);
      const first = (this.page - 1) * size;
      const onPage = new Set(matching.slice(first, first + size));

      this.rows.forEach((row) => { row.hidden = !onPage.has(row); });

      if (this.count) {
        const total = this.rows.length;
        const of = visible === total
          ? `${total} ${total === 1 ? 'record' : 'records'}`
          : `${visible} of ${total} records`;
        this.count.textContent = pages > 1
          ? `Showing ${first + 1}–${Math.min(first + size, visible)} of ${of}`
          : of;
      }

      if (this.pager) {
        // Nothing to page through: a pager would only be clutter.
        const needed = visible > PAGE_SIZES[0];
        this.pager.hidden = !needed;
        if (this.madeFoot) this.madeFoot.hidden = !needed;
        this.pageLabel.textContent = `Page ${this.page} of ${pages}`;
        this.prevBtn.disabled = this.page <= 1;
        this.nextBtn.disabled = this.page >= pages;
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
      // The order changed, so the page has to be cut again from the top.
      this.apply();
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
