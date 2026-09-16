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

      this.toast(n.message, n.tone);
    },

    /* An alert that arrives over the socket has to look and behave exactly
       like one rendered by partials/topbar.html -- same classes, and a link
       when it leads somewhere. A row that was styled differently, or that
       alone refused to open, would read as a glitch. */
    buildRow(list, n) {
      const tone = n.tone === 'danger' || n.tone === 'warning' ? n.tone : 'info';
      const template = list.dataset.notificationOpen || '';
      const role = list.dataset.viewerRole || '';
      const linkable = template && n.id
        && Array.isArray(n.link_roles) && n.link_roles.indexOf(role) !== -1;

      const row = document.createElement(linkable ? 'a' : 'div');
      row.className = 'pop__item pop__item--unread' + (linkable ? ' pop__item--link' : '');
      if (linkable) row.href = template.replace('__id__', encodeURIComponent(n.id));

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

      row.append(mark, body);
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
