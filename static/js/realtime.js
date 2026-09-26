/* ==========================================================================
   realtime.js -- the Socket.IO client

   Rooms are joined by the server from the session, so this file never asks
   for one. It connects, listens, and updates what is on screen.

   Everything here is an enhancement. If the socket never connects, the pages
   still work: the map polls /api/live/vehicles every 30 seconds and the rest
   of the data is correct as rendered. So a failure to connect degrades to
   "slightly less live" rather than to broken, and is reported quietly.
   ========================================================================== */

(function () {
  'use strict';

  if (typeof io === 'undefined') return;   // socket.io script blocked or offline

  const $  = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  /* ---- Notification focus (phones) ---------------------------------------
     A new alert on a phone takes the screen: the page blurs and the alert
     comes forward as a card, until it is viewed or dismissed. Alerts that
     arrive together wait their turn ("1 of 3") rather than stacking. Only
     below 760px -- a desktop keeps the toast. Notifications only; nothing
     else on the site uses this. */
  const NoticeFocus = {
    queue: [],
    node: null,
    lastFocus: null,

    phone() {
      return window.matchMedia('(max-width: 760px)').matches;
    },

    show(n, href) {
      if (!n || !this.phone()) return false;
      this.queue.push({ n, href });
      if (!this.node) this.next();
      else this.updateCount();
      return true;
    },

    next() {
      const item = this.queue.shift();
      if (!item) return this.close();
      const { n, href } = item;
      const tone = ['danger', 'warning', 'success'].includes(n.tone) ? n.tone : 'info';

      if (!this.node) {
        this.lastFocus = document.activeElement;
        this.node = document.createElement('div');
        this.node.className = 'notice-focus';
        this.node.setAttribute('role', 'alertdialog');
        this.node.setAttribute('aria-modal', 'true');
        this.node.setAttribute('aria-labelledby', 'notice-focus-title');
        this.node.setAttribute('aria-describedby', 'notice-focus-body');
        // A tap on the blurred page, outside the card, dismisses it.
        this.node.addEventListener('click', (e) => {
          if (e.target === this.node) this.dismiss();
        });
        document.addEventListener('keydown', this.onKey = (e) => {
          if (e.key === 'Escape') this.dismiss();
        });
        document.body.appendChild(this.node);
        document.body.classList.add('is-notice-focused');
      }

      // Built with textContent throughout: a title or message can carry a
      // barangay or owner name, and innerHTML would make that markup.
      const card = document.createElement('div');
      card.className = `notice-focus__card notice-focus__card--${tone}`;

      const head = document.createElement('div');
      head.className = 'notice-focus__head';
      const icon = document.createElement('span');
      icon.className = 'notice-focus__icon';
      icon.setAttribute('aria-hidden', 'true');
      const bell = document.querySelector('.bell svg');
      if (bell) icon.appendChild(bell.cloneNode(true));

      const text = document.createElement('div');
      text.style.cssText = 'flex:1;min-width:0';
      const kicker = document.createElement('p');
      kicker.className = 'notice-focus__kicker';
      kicker.dataset.noticeCount = '';
      const title = document.createElement('p');
      title.className = 'notice-focus__title';
      title.id = 'notice-focus-title';
      title.textContent = n.title || 'New notification';
      const body = document.createElement('p');
      body.className = 'notice-focus__body';
      body.id = 'notice-focus-body';
      body.textContent = n.message || '';
      text.append(kicker, title, body);
      head.append(icon, text);

      const actions = document.createElement('div');
      actions.className = 'notice-focus__actions';
      const dismiss = document.createElement('button');
      dismiss.type = 'button';
      dismiss.className = 'btn btn--secondary';
      dismiss.textContent = this.queue.length ? 'Next' : 'Dismiss';
      dismiss.addEventListener('click', () => this.dismiss());
      actions.appendChild(dismiss);
      if (href) {
        const view = document.createElement('a');
        view.className = 'btn btn--primary';
        view.href = href;
        view.textContent = 'View';
        actions.appendChild(view);
      }

      card.append(head, actions);
      this.node.replaceChildren(card);
      this.updateCount();

      // Two frames, so the opening state is painted before it animates.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (this.node) this.node.dataset.open = 'true';
      }));
      (href ? actions.lastChild : dismiss).focus({ preventScroll: true });
    },

    updateCount() {
      if (!this.node) return;
      const kicker = this.node.querySelector('[data-notice-count]');
      if (kicker) {
        kicker.textContent = this.queue.length
          ? `New alert · ${this.queue.length} more waiting`
          : 'New alert';
      }
      const dismiss = this.node.querySelector('.notice-focus__actions .btn--secondary');
      if (dismiss) dismiss.textContent = this.queue.length ? 'Next' : 'Dismiss';
    },

    dismiss() {
      if (this.queue.length) this.next();
      else this.close();
    },

    close() {
      if (!this.node) return;
      const node = this.node;
      this.node = null;
      node.dataset.open = 'false';
      document.body.classList.remove('is-notice-focused');
      if (this.onKey) document.removeEventListener('keydown', this.onKey);
      // Let the fade-out play before removing it.
      setTimeout(() => node.remove(), 260);
      if (this.lastFocus && this.lastFocus.focus) this.lastFocus.focus({ preventScroll: true });
    },
  };

  window.GCTSNoticeFocus = NoticeFocus;

  const Live = {
    socket: null,
    connected: false,

    init() {
      this.socket = io({
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 10000,
      });

      this.socket.on('connect', () => {
        this.connected = true;
        this.setIndicator(true);
        // Rooms are re-derived server-side; we only say "I am back".
        this.socket.emit('rejoin');
      });

      this.socket.on('disconnect', () => {
        this.connected = false;
        this.setIndicator(false);
      });

      this.socket.on('connect_error', () => this.setIndicator(false));

      this.socket.on('notification_new', (n) => this.onNotification(n));
      this.socket.on('notification_count', (d) => this.setBadge(d.unread));

      // Anything that changes a counter refreshes the figures rather than
      // trying to patch them in place: a counter derived from several records
      // is easy to get subtly wrong by incrementing, and the request is cheap.
      ['collection_saved', 'mrf_pickup_saved', 'delivery_saved',
       'carry_over_created'].forEach((event) => {
        this.socket.on(event, () => this.refreshCounters());
      });

      this.socket.on('schedule_updated', () => {
        this.toast('The waste schedule has been updated. Reload to see it.');
      });

      this.socket.on('location_update', (v) => {
        document.dispatchEvent(new CustomEvent('gcts:location', { detail: v }));
      });
      this.socket.on('collector_status', (v) => {
        document.dispatchEvent(new CustomEvent('gcts:duty', { detail: v }));
        this.refreshCounters();
      });

      this.wireDutyStream();
    },

    /* ---- Connection indicator ------------------------------------------ */

    setIndicator(live) {
      $$('[data-live-indicator]').forEach((el) => {
        el.dataset.liveState = live ? 'on' : 'off';
        el.title = live
          ? 'Live updates connected'
          : 'Live updates unavailable — the page refreshes every 30 seconds instead';
      });
    },

    /* ---- Notifications -------------------------------------------------- */

    onNotification(n) {
      const badge = $('[data-unread-count]');
      if (badge) this.setBadge((Number(badge.textContent) || 0) + 1);

      const list = $('[data-notification-list]');
      if (list) list.prepend(this.buildRow(list, n));

      // On a phone the alert takes focus; on a desktop it stays a toast.
      if (!NoticeFocus.show(n, this.linkFor(n))) this.toast(n.message, n.tone);
    },

    /* Where tapping this alert goes, or '' when it leads nowhere for this
       viewer. Shared by the bell row and the focused card, so both agree. */
    linkFor(n) {
      const list = $('[data-notification-list]');
      if (!list || !n || !n.id) return '';
      const template = list.dataset.notificationOpen || '';
      const role = list.dataset.viewerRole || '';
      const linkable = template
        && Array.isArray(n.link_roles) && n.link_roles.indexOf(role) !== -1;
      return linkable ? template.replace('__id__', encodeURIComponent(n.id)) : '';
    },

    /* An alert that arrives over the socket has to look and behave exactly
       like one rendered by partials/topbar.html -- same classes, and a link
       when it leads somewhere. A row that was styled differently, or that
       alone refused to open, would read as a glitch. */
    buildRow(list, n) {
      const tone = n.tone === 'danger' || n.tone === 'warning' ? n.tone : 'info';
      const href = this.linkFor(n);
      const linkable = Boolean(href);

      const row = document.createElement(linkable ? 'a' : 'div');
      row.className = 'pop__item pop__item--unread' + (linkable ? ' pop__item--link' : '');
      if (linkable) row.href = href;

      const mark = document.createElement('span');
      mark.className = `stat__icon stat__icon--${tone}`;
      mark.style.cssText = 'width:32px;height:32px;border-radius:10px';

      const body = document.createElement('div');
      body.style.cssText = 'flex:1;min-width:0';
      // textContent throughout: a title or message can carry a barangay or
      // owner name, and innerHTML would make that markup.
      const title = document.createElement('p');
      title.className = 'alert-item__title';
      title.textContent = n.title || '';
      const text = document.createElement('p');
      text.className = 'alert-item__body';
      text.textContent = n.message || '';
      body.append(title, text);

      // The glyph, the time and the arrow the server-rendered rows carry.
      // Icons are drawn server-side, so the bell's own glyph stands in; the
      // arrow is copied from a rendered row when one is on the page.
      const bell = $('.bell svg');
      if (bell) {
        const glyph = bell.cloneNode(true);
        glyph.setAttribute('class', 'icon icon--sm');
        mark.appendChild(glyph);
      }
      const time = document.createElement('time');
      time.className = 'alert-item__time';
      time.textContent = 'Just now';

      row.append(mark, body, time);
      if (linkable) {
        const go = $('.pop__item__go', list);
        if (go) row.appendChild(go.cloneNode(true));
      }
      return row;
    },

    setBadge(count) {
      $$('[data-unread-count]').forEach((el) => {
        el.textContent = count;
        el.hidden = !count;
      });
    },

    /* ---- Counters ------------------------------------------------------- */

    refreshCounters() {
      // Debounced: a truck finishing a route can fire several events within a
      // second, and one refresh covers them all.
      clearTimeout(this._refreshTimer);
      this._refreshTimer = setTimeout(() => {
        const marker = $('[data-live-refresh]');
        if (marker) marker.dataset.stale = 'true';
        document.dispatchEvent(new CustomEvent('gcts:counters'));
      }, 800);
    },

    /* ---- Duty position stream ------------------------------------------ */

    wireDutyStream() {
      const card = $('[data-duty-card]');
      if (!card || card.dataset.dutyState !== 'on') return;
      if (!('geolocation' in navigator)) return;

      const intervalMs = (Number(card.dataset.dutyInterval) || 8) * 1000;
      let lastSent = 0;

      // The socket carries positions while it is up; app.js keeps POSTing as
      // the fallback, and the server treats both the same way.
      navigator.geolocation.watchPosition(
        (pos) => {
          if (!this.connected) return;
          const now = Date.now();
          if (now - lastSent < intervalMs) return;
          lastSent = now;
          this.socket.emit('location_update', {
            lat: pos.coords.latitude,
            lng: pos.coords.longitude,
            accuracy: pos.coords.accuracy,
          });
        },
        () => {},
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
      );
    },

    /* ---- Toast ---------------------------------------------------------- */

    toast(message, tone) {
      if (!message) return;
      const host = $('[data-toast-host]') || document.body;
      const node = document.createElement('div');
      node.className = `toast toast--${tone || 'info'}`;
      node.setAttribute('role', 'status');
      node.textContent = message;
      host.appendChild(node);
      setTimeout(() => node.remove(), 6000);
    },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Live.init());
  } else {
    Live.init();
  }

  window.GCTSLive = Live;
})();
