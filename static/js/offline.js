/* ==========================================================================
   offline.js -- hold an unsent collection entry and retry it

   Collectors work outdoors, on mobile data, often out of signal. Losing a
   stop record because the phone dropped its connection mid-submit means the
   collector has to remember and re-enter it later, which in practice means it
   is lost.

   So: if a record form is submitted while offline, the entry is kept in
   localStorage and sent when the connection returns.

   Two deliberate limits:

     * Photo proofs are NOT queued. A 5 MB image would blow the ~5 MB
       localStorage budget for a single entry. A not-collected record needs
       its photo, so those still require a live connection, and the collector
       is told so plainly rather than being left to think it saved.
     * A queued entry expires after 24 hours. Re-submitting a stop from
       several days ago would file it under today's date and corrupt both
       days' totals.
   ========================================================================== */

(function () {
  'use strict';

  const KEY = 'gcts-queue';
  const MAX_AGE_MS = 24 * 60 * 60 * 1000;

  const Offline = {
    init() {
      this.banner = document.querySelector('[data-offline-banner]');
      this.wireForms();
      this.render();

      window.addEventListener('online', () => { this.render(); this.flush(); });
      window.addEventListener('offline', () => this.render());

      if (navigator.onLine) this.flush();
    },

    /* ---- Queue ---------------------------------------------------------- */

    read() {
      try {
        const rows = JSON.parse(localStorage.getItem(KEY) || '[]');
        const fresh = rows.filter((r) => Date.now() - r.queued_at < MAX_AGE_MS);
        if (fresh.length !== rows.length) this.write(fresh);
        return fresh;
      } catch (err) {
        return [];
      }
    },

    write(rows) {
      try {
        localStorage.setItem(KEY, JSON.stringify(rows));
      } catch (err) {
        // Storage full or blocked (private browsing). Nothing useful to do —
        // the form will simply fail loudly instead, which is honest.
      }
    },

    add(entry) {
      const rows = this.read();
      // One queued entry per stop: a correction should replace the pending
      // record, not queue behind it.
      const kept = rows.filter((r) => r.url !== entry.url);
      kept.push(entry);
      this.write(kept);
      this.render();
    },

    /* ---- Forms ---------------------------------------------------------- */

    wireForms() {
      document.querySelectorAll('form[data-offline-queue]').forEach((form) => {
        form.addEventListener('submit', (event) => {
          if (navigator.onLine) return;         // normal submit

          const data = new FormData(form);
          const proof = data.get('proof');
          if (proof && proof.size) {
            event.preventDefault();
            this.tell('You are offline and this entry has a photo. Photos '
                      + 'cannot be saved offline — please try again when you '
                      + 'have a signal.', 'danger');
            return;
          }

          event.preventDefault();
          const fields = {};
          data.forEach((value, key) => {
            if (typeof value === 'string') fields[key] = value;
          });

          this.add({
            url: form.getAttribute('action') || window.location.pathname,
            fields,
            label: form.dataset.offlineLabel || 'Collection entry',
            queued_at: Date.now(),
          });

          this.tell('You are offline. This entry is saved on your phone and '
                    + 'will be sent automatically when you reconnect.', 'warning');
          form.reset();
        });
      });
    },

    /* ---- Sending -------------------------------------------------------- */

    async flush() {
      const rows = this.read();
      if (!rows.length) return;

      const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
      const remaining = [];

      for (const row of rows) {
        const body = new URLSearchParams(row.fields);
        body.set('csrf_token', token);
        try {
          const res = await fetch(row.url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body,
            redirect: 'follow',
          });
          // A 4xx means the server rejected it on its merits (a stale entry,
          // a locked day). Retrying forever would never fix that, so it is
          // dropped and reported rather than left to loop.
          if (!res.ok && res.status >= 500) remaining.push(row);
          else if (!res.ok) {
            this.tell(`"${row.label}" could not be saved and has been discarded. `
                      + 'Please record it again.', 'danger');
          }
        } catch (err) {
          remaining.push(row);               // still offline
        }
      }

      this.write(remaining);
      this.render();

      if (rows.length > remaining.length) {
        const sent = rows.length - remaining.length;
        this.tell(`${sent} saved entr${sent === 1 ? 'y has' : 'ies have'} been `
                  + 'sent now that you are back online.', 'success');
      }
    },

    /* ---- Feedback ------------------------------------------------------- */

    render() {
      if (!this.banner) return;
      const pending = this.read().length;
      const offline = !navigator.onLine;

      this.banner.hidden = !offline && !pending;
      if (this.banner.hidden) return;

      const text = this.banner.querySelector('[data-offline-text]');
      if (!text) return;

      if (offline && pending) {
        text.textContent = `You are offline. ${pending} entr`
          + `${pending === 1 ? 'y is' : 'ies are'} saved on this phone and will `
          + 'be sent when you reconnect.';
      } else if (offline) {
        text.textContent = 'You are offline. Entries you record will be saved '
          + 'on this phone and sent when you reconnect.';
      } else {
        text.textContent = `Sending ${pending} saved entr`
          + `${pending === 1 ? 'y' : 'ies'}…`;
      }
    },

    tell(message, tone) {
      const host = document.querySelector('[data-toast-host]') || document.body;
      const node = document.createElement('div');
      node.className = `toast toast--${tone || 'info'}`;
      node.setAttribute('role', 'status');
      node.textContent = message;
      host.appendChild(node);
      setTimeout(() => node.remove(), 8000);
    },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Offline.init());
  } else {
    Offline.init();
  }
})();
