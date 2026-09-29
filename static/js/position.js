/* ==========================================================================
   position.js -- one GPS watcher for the whole page

   A collector's page has three things that want the phone's position: the
   duty tracker that POSTs it (app.js), the socket that streams it
   (realtime.js), and their own map that follows it (map.js). Each used to
   open its own watchPosition, and a browser juggling three high-accuracy
   watchers does not reliably feed all of them -- the map's, opened last, could
   wait indefinitely for a first fix while the other two already had one.

   So there is one watcher, opened on the first subscription, and every
   subscriber gets every fix. A late subscriber is handed the last fix (or the
   last error) straight away rather than waiting for the device to move.

   Loaded before the other scripts, with nothing to do until something calls
   GCTSPosition.watch().
   ========================================================================== */

(function () {
  'use strict';

  const subscribers = new Set();
  let watchId = null;
  let lastFix = null;
  let lastError = null;

  function start() {
    if (watchId !== null || !('geolocation' in navigator)) return;
    watchId = navigator.geolocation.watchPosition(
      (pos) => {
        lastFix = pos;
        lastError = null;
        subscribers.forEach((s) => s.onFix(pos));
      },
      (err) => {
        lastError = err;
        subscribers.forEach((s) => { if (s.onError) s.onError(err); });
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 });

    window.addEventListener('pagehide', () => {
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
      watchId = null;
    });
  }

  window.GCTSPosition = {
    supported: 'geolocation' in navigator,

    /* Subscribe to the device's position. Returns an unsubscribe function. */
    watch(onFix, onError) {
      const sub = { onFix, onError };
      subscribers.add(sub);
      start();
      // Replay what is already known, asynchronously, so a caller always
      // finishes its own setup before its first callback runs.
      if (lastFix) setTimeout(() => subscribers.has(sub) && onFix(lastFix), 0);
      else if (lastError && onError) {
        setTimeout(() => subscribers.has(sub) && onError(lastError), 0);
      }
      return () => subscribers.delete(sub);
    },
  };
})();
