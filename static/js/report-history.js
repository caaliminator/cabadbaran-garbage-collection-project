/* History & Reports: a day card fills the report form's dates.

   Click a day and the Starting and End Date both become that day -- the card
   already says which day it is, so retyping it into two date pickers was the
   slow part of reporting on it. Shift-click a second day and the range runs
   between the two, in whichever order they were picked.

   Also: the "Show N days" picker submits itself, so changing the page size is
   one action rather than a change and a separate Apply. */
(function () {
  'use strict';

  const list = document.querySelector('[data-report-days]');
  const form = document.getElementById('report-form');
  const start = form && form.querySelector('#start_date');
  const end = form && form.querySelector('#end_date');

  document.querySelectorAll('select[data-autosubmit]').forEach((select) => {
    select.addEventListener('change', () => select.form && select.form.submit());
  });

  if (!list || !start || !end) return;

  let anchor = null;     // the day a Shift-click range is measured from

  function mark(from, to) {
    list.querySelectorAll('[data-report-day]').forEach((card) => {
      const day = card.dataset.reportDay;
      const on = day >= from && day <= to;
      card.classList.toggle('is-selected', on);
      card.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function pick(card, extend) {
    const day = card.dataset.reportDay;
    if (!day) return;

    let from = day;
    let to = day;
    if (extend && anchor) {
      from = anchor < day ? anchor : day;
      to = anchor < day ? day : anchor;
    } else {
      anchor = day;
    }

    start.value = from;
    end.value = to;
    // Let the form's own validation and anything listening see the change.
    [start, end].forEach((input) =>
      input.dispatchEvent(new Event('change', { bubbles: true })));
    mark(from, to);

    // On a narrow screen the form is below the list; bring it into view so
    // the admin sees what just happened. On a wide one it is already beside
    // the list, and scrolling would only be a jolt.
    if (window.matchMedia('(max-width: 960px)').matches) {
      form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // A Shift-click is also the browser's "extend the text selection", which
  // painted every card between the two in selection blue. Suppressed for a
  // Shift-press on a card only, so ordinary copying of a card's text still
  // works.
  list.addEventListener('mousedown', (event) => {
    if (event.shiftKey && !event.target.closest('a, button')
        && event.target.closest('[data-report-day]')) {
      event.preventDefault();
    }
  });

  // A link or button inside a card (View details) is its own action: it
  // must not also fill the report dates, and Enter on it must follow it.
  const ownAction = (target) => target.closest('a, button');

  list.addEventListener('click', (event) => {
    if (ownAction(event.target)) return;
    const card = event.target.closest('[data-report-day]');
    if (card) pick(card, event.shiftKey);
  });

  // role="button" promises the keyboard behaviour of one.
  list.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (ownAction(event.target)) return;
    const card = event.target.closest('[data-report-day]');
    if (!card) return;
    event.preventDefault();
    pick(card, event.shiftKey);
  });

  // Coming back to the page with dates already in the form -- after
  // generating a report -- shows which days that report covered.
  if (start.value && end.value) mark(start.value, end.value);
})();
