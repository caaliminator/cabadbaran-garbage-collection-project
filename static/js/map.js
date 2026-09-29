/* ==========================================================================
   map.js -- the live tracking map

   Leaflet, loaded by CDN. Every coordinate, boundary, and colour comes from
   the server at runtime:

       /api/geo/config      centre, zoom, tiles
       /api/geo/barangays   barangay boundary polygons
       /api/geo/mrfs        one MRF per barangay
       /api/geo/hotspots    the optional hotspot overlay
       /api/live/vehicles   on-duty tricycles and trucks

   There is deliberately no coordinate in this file. Dropping the real
   barangay boundaries into data/geo/ is meant to require no code change at
   all, and a hardcoded fallback here would quietly defeat that.

   Nothing is painted over the barangays themselves: no fill and no boundary
   line. The geometry is still loaded, because it is what the map frames
   itself on -- the city on open, one barangay on a scoped page -- but it is
   never drawn, so the basemap underneath is read directly.

   The MRFs are drawn, and follow the barangay filter: with a barangay chosen
   (or a page locked to one) only that barangay's MRF shows, so a resident
   sees where their own waste goes. The MRF worklist, with its pickup status,
   still lives on the MRF page.

   Layer order matters: hotspots sit underneath, MRFs above them, live
   vehicles on top -- so no overlay ever obscures a moving truck.
   ========================================================================== */

(function () {
  'use strict';

  const REFRESH_MS = 30000;   // socket takes over in Phase 7; this is the fallback

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  const LiveMap = {
    init() {
      document.querySelectorAll('[data-map]').forEach((node) => this.build(node));
    },

    async build(node) {
      if (typeof L === 'undefined') {
        this.fail(node, 'The map library could not be loaded. Check the connection and reload.');
        return;
      }

      let config;
      try {
        config = await fetch('/api/geo/config').then((r) => r.json());
      } catch (err) {
        this.fail(node, 'The map settings could not be loaded.');
        return;
      }

      const canvas = node.querySelector('[data-map-canvas]');
      const map = L.map(canvas, {
        center: config.center,
        zoom: config.zoom,
        scrollWheelZoom: false,   // a page-scroll should not zoom the map by accident
      });
      map.on('click', () => map.scrollWheelZoom.enable());
      map.on('mouseout', () => map.scrollWheelZoom.disable());

      const baseLayers = this.basemaps(config, map);

      const hotspotLayer = L.layerGroup();
      const mrfLayer = L.layerGroup().addTo(map);
      const vehicleLayer = L.layerGroup().addTo(map);

      const state = {
        node, map, hotspotLayer, mrfLayer, vehicleLayer,
        config,
        filter: node.dataset.mapFilter || 'all',
        barangay: node.dataset.mapBarangay || '',
        scope: node.dataset.mapScope || '',
        locateZoom: Number(node.dataset.mapLocate) || 0,
        followVehicle: node.dataset.mapFollow || '',
        // A fixed set of MRFs this map is about (a truck's route), on top of
        // whatever barangay filter is chosen. Empty means every MRF.
        mrfScope: (node.dataset.mapMrfs || '').split(',').filter(Boolean),
        markers: {},
      };

      // A drag or a pinch means the viewer has taken over. Geolocation is
      // slow -- a cold GPS fix can take ten seconds -- and yanking the map
      // out from under someone who is already reading it is worse than not
      // centring on them at all.
      state.map.on('dragstart', () => { state.userMoved = true; });

      await this.loadZones(state);

      if (state.followVehicle) this.followMe(state);
      else if (state.locateZoom) this.locate(state);

      // A page can leave the MRFs off altogether -- the tricycle collector's
      // map, whose round ends at the household, not the facility.
      const withMrfs = !('mapNoMrfs' in node.dataset);
      const overlays = withMrfs ? { MRFs: mrfLayer } : {};
      overlays['Live vehicles'] = vehicleLayer;
      const control = L.control.layers(baseLayers, overlays,
                                       { collapsed: true }).addTo(map);

      if (withMrfs) await this.loadMrfs(state);
      else map.removeLayer(mrfLayer);

      if (config.hotspot_layer_enabled) {
        control.addOverlay(hotspotLayer, 'Hotspots');
        await this.drawHotspots(state);
      }

      L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);

      this.wireControls(state);
      this.wireLiveUpdates(state);
      await this.drawVehicles(state);

      // Still polling. The socket is the fast path, not the only one: it can
      // be down, and a client that has just reconnected has missed whatever
      // moved while it was away.
      setInterval(() => this.drawVehicles(state), REFRESH_MS);
    },

    /* Build the selectable basemaps and add the default one to the map.

       Which providers exist is the server's business, not this file's --
       swapping OpenStreetMap for a keyed provider is an edit to
       Config.MAP_BASEMAPS and nothing here. The single-URL response is still
       honoured so an older/simpler config keeps working.

       A satellite layer is imagery *plus* its place-name tiles: imagery on
       its own is detailed and unnavigable, because nothing on it is named. */
    basemaps(config, map) {
      const defined = (config.basemaps || []).length
        ? config.basemaps
        : [{ label: 'Streets', url: config.tile_url,
             attribution: config.attribution, max_zoom: 19, default: true }];

      const layers = {};
      let initial = null;
      defined.forEach((base) => {
        const maxZoom = base.max_zoom || 19;
        // maxNativeZoom only when the provider is shallower than the map:
        // passing undefined leaves Leaflet's default (request every level).
        const maxNativeZoom = base.max_native_zoom || undefined;
        const tiles = L.tileLayer(base.url, {
          attribution: base.attribution,
          maxZoom,
          maxNativeZoom,
        });
        layers[base.label] = base.label_url
          ? L.layerGroup([tiles,
                          L.tileLayer(base.label_url, { maxZoom, maxNativeZoom })])
          : tiles;
        if (!initial || base.default) initial = layers[base.label];
      });

      if (initial) initial.addTo(map);
      return layers;
    },

    fail(node, message) {
      const canvas = node.querySelector('[data-map-canvas]');
      if (!canvas) return;
      canvas.innerHTML = '';
      const box = el('div', 'map__empty');
      box.appendChild(el('p', 'strong', 'Map unavailable'));
      box.appendChild(el('p', 'text-sm', message));
      canvas.appendChild(box);
    },

    note(state, message) {
      const slot = state.node.querySelector('[data-map-note]');
      if (slot) slot.textContent = message || '';
    },

    /* ---- Layers -------------------------------------------------------- */

    /* Load the barangay boundaries without drawing them.

       The polygons are built but never added to the map: they exist only as
       bounds, which is what frames the view -- the whole city on a public
       map, the one barangay a scoped page belongs to. Every mark they used to
       make (the coloured fill, then the outline that replaced it) is gone, so
       the streets, landmarks and building shapes on the basemap come through
       unobstructed, and a vehicle's pin is the only thing on top of them.

       Leaflet computes a polygon's bounds from its coordinates, so this works
       on a layer that was never added to a map. */
    async loadZones(state) {
      const data = await fetch('/api/geo/barangays').then((r) => r.json());

      const drawn = L.geoJSON(data, { interactive: false });
      state.zoneLayers = {};
      drawn.eachLayer((layer) => {
        const id = layer.feature?.properties?.barangay_id;
        if (id) state.zoneLayers[id] = layer;
      });

      if (data.meta.with_geometry > 0) {
        state.cityBounds = drawn.getBounds();

        // Framed without animation. Leaflet silently ignores a setView made
        // while a zoom animation is still running, and a collector's cached
        // GPS fix arrives inside that quarter-second -- so an animated opening
        // swallowed the zoom onto their own position.
        if (state.barangay && state.zoneLayers[state.barangay]) {
          this.focusBarangay(state, state.barangay, { animate: false });
        } else if (state.cityBounds.isValid()) {
          state.map.fitBounds(state.cityBounds, { padding: [16, 16], animate: false });
        }

        // Placeholder geometry no longer shows on the map, but it still
        // decides where the map opens, so the caveat stands.
        if (data.meta.placeholder) {
          this.note(state,
            'Barangay outlines are illustrative shapes drawn around each '
            + 'barangay\'s published coordinate, not surveyed boundaries. '
            + 'They decide where the map opens and nothing else. '
            + 'See docs/DATA_REQUIREMENTS.md.');
        }
      } else {
        // Without geometry there is nothing to frame on, so the map stays on
        // the configured city centre -- say why rather than leaving it
        // looking arbitrary.
        this.note(state,
          `Barangay boundaries have not been loaded yet (${data.meta.total} barangays known). ` +
          'See docs/DATA_REQUIREMENTS.md.');
      }
    },

    /* Open on the viewer's own position, when they allow it.

       Deliberately not L.Map.locate(): that fires `locationerror` on every
       refusal, and a resident declining the prompt is a normal answer, not a
       fault to report. This asks once, moves the map if it gets an answer,
       and otherwise leaves the citywide view exactly as it was.

       The browser only offers geolocation on a secure origin -- https, or
       localhost while developing. Over plain http on a LAN the callback
       never fires, which is another reason the city view has to stand on its
       own rather than being a placeholder for this.  */
    locate(state) {
      if (!navigator.geolocation) return;

      const done = (message) => this.note(state,
        [state.node.querySelector('[data-map-note]')?.textContent, message]
          .filter(Boolean).join(' '));

      navigator.geolocation.getCurrentPosition(
        (position) => {
          if (state.userMoved) return;
          const { latitude: lat, longitude: lng, accuracy } = position.coords;

          state.map.setView([lat, lng], state.locateZoom);

          // A dot plus its accuracy circle, so a 2 km fix does not read as a
          // doorstep-precise one. Both are plain Leaflet shapes -- there is
          // no marker image to load and nothing to go missing offline.
          L.circle([lat, lng], {
            radius: Math.max(accuracy || 0, 25),
            className: 'map-here__halo',
            weight: 1,
          }).addTo(state.map);

          L.circleMarker([lat, lng], {
            radius: 7,
            className: 'map-here__dot',
            weight: 3,
          })
            .bindPopup('You are here')
            .addTo(state.map);
        },
        () => {
          // Denied, unavailable, or timed out. All three mean the same thing
          // to the map: stay on the city.
          done('Showing the whole city — location sharing is off.');
        },
        { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
    },

    /* A collector's own map, following their own phone.

       Their marker also arrives over the socket like everyone else's, but
       that round trip only happens while they are On Duty and only every few
       seconds. Watching the device directly means the map tracks them from
       the moment the page opens, off duty included, and keeps moving if the
       socket drops.

       `watchPosition`, not a `setInterval` around `getCurrentPosition`: the
       browser hands over a new fix when the device actually moves, which on
       a phone in a pocket is far cheaper than asking on a timer. */
    followMe(state) {
      if (!window.GCTSPosition || !window.GCTSPosition.supported) {
        this.note(state, 'This device cannot report its location.');
        return;
      }

      let marker = null;
      let halo = null;

      // The page's one GPS watcher (position.js), shared with the duty
      // tracker -- a second watcher of its own could wait forever for a fix.
      window.GCTSPosition.watch(
        (position) => {
          const { latitude: lat, longitude: lng, accuracy } = position.coords;
          const point = [lat, lng];

          if (!marker) {
            halo = L.circle(point, {
              radius: Math.max(accuracy || 0, 25),
              className: 'map-here__halo',
              weight: 1,
            }).addTo(state.map);
            marker = L.circleMarker(point, {
              radius: 8,
              className: 'map-here__dot',
              weight: 3,
            }).bindPopup('Your position').addTo(state.map);
          } else {
            marker.setLatLng(point);
            halo.setLatLng(point).setRadius(Math.max(accuracy || 0, 25));
          }

          // Snap to them once on the first fix; after that follow only while
          // they have not taken the map somewhere themselves.
          state.gpsFix = true;
          if (!state.centred) this.centreOnMe(state, point);
          else if (!state.userMoved) state.map.panTo(point);
        },
        (error) => {
          this.note(state, error && error.code === 1
            ? 'Location is blocked for this site. The map cannot follow you '
              + 'until you allow it in your browser settings.'
            : 'Your location is unavailable right now.');
        });
    },

    /* The one zoom onto the collector: on their first GPS fix, or -- while
       On Duty -- on the position the server last had for their vehicle,
       whichever comes first. A cold GPS fix can take ten seconds; the
       server's copy is usually seconds old and arrives with the first poll. */
    centreOnMe(state, point) {
      if (state.userMoved) return;
      state.map.setView(point, state.locateZoom || 16, { animate: false });
      state.centred = true;
    },

    /* Zoom to one barangay. With nothing drawn there is no shape to
       highlight and no neighbour to fade -- the view itself is what says
       which barangay is being looked at. */
    focusBarangay(state, barangayId, options) {
      const target = (state.zoneLayers || {})[barangayId];
      if (target && target.getBounds) {
        state.map.fitBounds(target.getBounds(),
                            { padding: [40, 40], maxZoom: 15, ...options });
      }
    },

    clearFocus(state) {
      if (state.cityBounds && state.cityBounds.isValid()) {
        state.map.fitBounds(state.cityBounds, { padding: [16, 16] });
      }
    },

    async drawHotspots(state) {
      const params = state.barangay ? `?barangay=${encodeURIComponent(state.barangay)}` : '';
      const data = await fetch(`/api/geo/hotspots${params}`).then((r) => r.json());
      state.hotspotLayer.clearLayers();

      if (!data.features.length) return;

      data.features.forEach((f) => {
        const p = f.properties;
        const [lng, lat] = f.geometry.coordinates;
        L.circleMarker([lat, lng], {
          className: `map-hotspot map-hotspot--${p.severity}`,
          radius: p.severity === 'high' ? 16 : p.severity === 'medium' ? 12 : 9,
          weight: 1,
        })
          .bindPopup(
            `<strong>${p.barangay_name || ''} — ${p.purok}</strong>` +
            `<br>Severity: ${p.severity}` +
            `<br>${p.notes || ''}` +
            (p.last_reported ? `<br>Last reported: ${p.last_reported}` : '')
          )
          .addTo(state.hotspotLayer);
      });
    },

    /* Fetch the MRFs once; the barangay filter only changes which are drawn,
       so switching it needs no round trip. */
    async loadMrfs(state) {
      try {
        const data = await fetch('/api/geo/mrfs').then((r) => r.json());
        state.mrfs = (data.mrfs || []).filter((m) => m.located);
      } catch (err) {
        state.mrfs = [];   // the map still works without them
      }
      this.drawMrfs(state);
    },

    /* The MRFs in scope right now: the chosen barangay's alone, or every one
       the page is about when none is chosen. */
    drawMrfs(state) {
      state.mrfLayer.clearLayers();

      const shown = (state.mrfs || []).filter((m) =>
        (!state.barangay || m.barangay_id === state.barangay)
        && (!state.mrfScope.length || state.mrfScope.includes(m.barangay_id)));

      shown.forEach((m) => {
        L.marker([m.lat, m.lng], {
          icon: this.mrfIcon(m),
          // Below every vehicle, whatever their latitudes: a truck parked at
          // its MRF has to stay the thing that is visible.
          zIndexOffset: -1000,
          title: m.name || 'MRF',
        })
          .bindPopup(
            `<strong>${m.name || 'MRF'}</strong>` +
            (m.barangay_name ? `<br>Barangay ${m.barangay_name}` : '') +
            `<br><span class="text-xs muted">${m.surveyed
              ? 'Surveyed location'
              : 'Approximate: placed at the barangay centre'}</span>`
          )
          .addTo(state.mrfLayer);
      });

      const legend = state.node.querySelector('[data-legend="mrfs"]');
      if (legend) legend.textContent = shown.length;
    },

    async drawVehicles(state) {
      const params = new URLSearchParams();
      if (state.filter && state.filter !== 'all') params.set('type', state.filter);
      if (state.barangay) params.set('barangay', state.barangay);

      let data;
      try {
        data = await fetch(`/api/live/vehicles?${params}`).then((r) => r.json());
      } catch (err) {
        return;   // a dropped poll is not worth disturbing the map for
      }

      // The poll is the authority on *which* vehicles belong on the map --
      // it is the only thing that ever removes one. Between polls the socket
      // moves the markers it already knows about.
      const seen = new Set();
      data.vehicles.forEach((v) => {
        seen.add(v.vehicle);
        this.placeVehicle(state, v);
      });
      Object.keys(state.markers).forEach((code) => {
        if (!seen.has(code)) this.dropVehicle(state, code);
      });

      state.counts = data.counts;
      this.updateLegend(state, data);
    },

    /* Put a vehicle on the map, or move the marker that is already there.

       Moving beats redrawing: a marker rebuilt from scratch every few seconds
       loses its open popup, flickers, and makes a truck look like it is
       teleporting rather than driving down a road. */
    placeVehicle(state, v) {
      if (v.lat == null || v.lng == null) return;

      // A collector on duty is on the map as soon as the server knows where
      // they are, even before this phone's GPS has answered.
      if (state.followVehicle && v.vehicle === state.followVehicle && !state.centred) {
        this.centreOnMe(state, [v.lat, v.lng]);
      }

      const existing = state.markers[v.vehicle];
      if (existing) {
        existing.setLatLng([v.lat, v.lng]);
        if (v.name || v.barangays) existing.setPopupContent(this.vehiclePopup(v));
        return existing;
      }

      const marker = L.marker([v.lat, v.lng], { icon: this.icon(v.kind, v.vehicle) })
        .bindPopup(this.vehiclePopup(v))
        .addTo(state.vehicleLayer);
      marker.gctsKind = v.kind;
      state.markers[v.vehicle] = marker;
      return marker;
    },

    clearVehicles(state) {
      Object.keys(state.markers).forEach((code) => this.dropVehicle(state, code));
    },

    dropVehicle(state, code) {
      const marker = state.markers[code];
      if (!marker) return;
      state.vehicleLayer.removeLayer(marker);
      delete state.markers[code];
    },

    vehiclePopup(v) {
      return `<strong>${v.vehicle}</strong>` +
        (v.name ? `<br>${v.name}` : '') +
        (v.barangays && v.barangays.length ? `<br>${v.barangays.join(', ')}` : '');
    },

    /* Does a live payload belong on this map as it is currently filtered?

       The socket broadcasts every vehicle in the city to the public room, so
       the filtering the API does server-side has to be repeated here --
       otherwise choosing a barangay would hold only until the next position
       arrived and put the whole fleet back. */
    passesFilter(state, v) {
      if (state.filter === 'tricycles' && v.kind !== 'tricycle') return false;
      if (state.filter === 'trucks' && v.kind !== 'truck') return false;
      if (state.barangay && !(v.barangay_ids || []).includes(state.barangay)) {
        return false;
      }
      return true;
    },

    /* Positions arriving over the socket, between polls. */
    wireLiveUpdates(state) {
      document.addEventListener('gcts:location', (event) => {
        const v = event.detail || {};
        if (!v.vehicle) return;

        if (!this.passesFilter(state, v)) {
          this.dropVehicle(state, v.vehicle);
          return;
        }
        this.placeVehicle(state, v);

        // A collector watching their own map rides along with the marker.
        // This device's own GPS is fresher than its echo off the server.
        if (state.followVehicle && v.vehicle === state.followVehicle
            && !state.userMoved && !state.gpsFix) {
          state.map.setView([v.lat, v.lng], state.map.getZoom());
        }
      });

      // Going off duty takes the marker away immediately rather than leaving
      // it parked until the next poll notices.
      document.addEventListener('gcts:duty', (event) => {
        const v = event.detail || {};
        if (!v.vehicle) return;
        if (v.on_duty) this.drawVehicles(state);
        else this.dropVehicle(state, v.vehicle);
      });
    },

    /* The tricycle and truck glyphs, copied from partials/icons.html so a pin
       and a table row show the same vehicle the same way. Paths only: the
       stroke is currentColor and every dimension comes from the CSS, which
       keeps this file free of colour as the rest of it is. */
    GLYPHS: {
      tricycle: '<circle cx="5.5" cy="17.5" r="3.5"/>'
        + '<circle cx="18.5" cy="17.5" r="3.5"/>'
        + '<path d="M5.5 17.5 9 7h4l3 6M9 7H7m8 10.5h-6"/>',
      truck: '<path d="M1 3h13v13H1zM14 8h4l3 3v5h-7"/>'
        + '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/>',
    },

    /* The MRF pin: the recycle glyph from partials/icons.html in a square
       badge, so it never reads as one of the round vehicle pills. An
       approximated position is drawn dashed -- right barangay, not
       necessarily the building. */
    mrfIcon(m) {
      return L.divIcon({
        className: `map-mrf${m.surveyed ? '' : ' map-mrf--approx'}`,
        html: '<span class="map-mrf__body">'
          + '<svg class="map-mrf__glyph" viewBox="0 0 24 24" aria-hidden="true" '
          + 'focusable="false">'
          + '<path d="M7 19H4.5a2 2 0 0 1-1.7-3l2.3-3.9M10.5 4.6l1.9-3.2a2 2 0 0 1 3.4 0l2.2 3.8"/>'
          + '<path d="m14 21 3.5-6H21a2 2 0 0 0 1.7-3l-1.6-2.8"/>'
          + '<path d="m8 8-3 5 5 3M17 21l-3-3 3-3M4.8 12.6 8 7"/>'
          + '</svg></span>',
        iconSize: [null, null],
      });
    },

    /* How many per-vehicle colours components.css defines. Kept in step with
       the .map-pin--vNN block there; a mismatch would only ever mean some
       colours go unused, never a pin without one. */
    COLOURS: 16,

    /* Which colour slot a vehicle owns, from its code alone.

       Deliberately not "the order they arrived in": that would hand TRI-04 a
       different colour on every reload, on every device, and to every viewer
       -- and a colour that will not sit still is worse than no colour. A hash
       of the code is stable everywhere, forever, with no server round trip and
       nothing to store.

       The multiplier is the usual small odd prime; the >>> 0 keeps it an
       unsigned 32-bit value, because a negative index names no class. */
    colourOf(code) {
      let hash = 0;
      for (let i = 0; i < code.length; i += 1) {
        hash = (hash * 31 + code.charCodeAt(i)) >>> 0;
      }
      return String(hash % this.COLOURS).padStart(2, '0');
    },

    /* A pin is the vehicle's glyph and its plate in one pill. The glyph says
       what kind of vehicle it is -- readable further off than the plate -- and
       the colour says which one, so two tricycles in the same purok are told
       apart at a glance. An unknown kind still gets its plate, just without a
       glyph; a vehicle with no code still gets its kind's colour. */
    icon(kind, label) {
      const glyph = this.GLYPHS[kind];
      const colour = label ? ` map-pin--v${this.colourOf(String(label))}` : '';
      return L.divIcon({
        className: `map-pin map-pin--${kind}${colour}`,
        html: '<span class="map-pin__body">'
          + (glyph
              ? '<svg class="map-pin__glyph" viewBox="0 0 24 24" '
                + `aria-hidden="true" focusable="false">${glyph}</svg>`
              : '')
          + `<span class="map-pin__label">${label}</span>`
          + '</span>',
        iconSize: [null, null],
      });
    },

    updateLegend(state, data) {
      const legend = state.node.querySelector('[data-map-legend]');
      if (!legend) return;

      const set = (key, value) => {
        const slot = legend.querySelector(`[data-legend="${key}"]`);
        if (slot) slot.textContent = value;
      };
      set('tricycles', data.counts.tricycles);
      set('trucks', data.counts.trucks);
      set('barangays', data.barangay_count);

      const stale = legend.querySelector('[data-legend="stale"]');
      if (stale) {
        stale.hidden = !data.counts.without_position;
        stale.textContent = data.counts.without_position
          ? `${data.counts.without_position} on duty without a recent position`
          : '';
      }

      this.describeFilter(state, data);
    },

    /* What the map is showing right now, in a sentence.

       A resident who picks their barangay and sees an empty map needs to know
       whether that means "nobody is collecting here yet today" or "the filter
       is broken". Counting is not enough; the empty case is the one that has
       to speak. */
    describeFilter(state, data) {
      const slot = state.node.querySelector('[data-map-summary]');
      if (!slot) return;

      const label = state.node.querySelector('[data-map-barangay]');
      const place = state.barangay && label
        ? label.options[label.selectedIndex].text
        : '';
      const kind = state.filter === 'tricycles' ? 'tricycle'
        : state.filter === 'trucks' ? 'truck' : 'vehicle';
      const shown = (state.filter === 'tricycles' ? data.counts.tricycles
        : state.filter === 'trucks' ? data.counts.trucks
        : data.counts.total) || 0;

      if (!place) {
        slot.textContent = shown
          ? `${shown} ${kind}${shown === 1 ? '' : 's'} working across the city.`
          : 'No collectors are on duty in the city right now.';
        return;
      }

      slot.textContent = shown
        ? `${shown} ${kind}${shown === 1 ? '' : 's'} working in ${place} right now.`
        : `No ${kind} is on duty in ${place} right now. `
          + 'The marker appears as soon as the collector starts their shift.';
    },

    /* ---- Controls ------------------------------------------------------ */

    wireControls(state) {
      state.node.querySelectorAll('[data-map-tab]').forEach((btn) => {
        btn.addEventListener('click', () => {
          state.node.querySelectorAll('[data-map-tab]').forEach((b) => {
            b.setAttribute('aria-selected', String(b === btn));
          });
          state.filter = btn.dataset.mapTab;
          this.clearVehicles(state);
          this.drawVehicles(state);
        });
      });

      const select = state.node.querySelector('[data-map-barangay]');
      if (select) {
        select.addEventListener('change', () => {
          state.barangay = select.value;
          // Drop what is on the map before asking for the new set: the old
          // barangay's vehicles are no longer in scope and should go now,
          // not whenever the reply happens to land.
          this.clearVehicles(state);
          // A deliberate choice of barangay is not the viewer drifting away
          // from their own position -- let the map fit to it.
          state.userMoved = false;
          this.drawVehicles(state);
          this.drawMrfs(state);
          if (state.config.hotspot_layer_enabled) this.drawHotspots(state);
          if (state.barangay) this.focusBarangay(state, state.barangay);
          else this.clearFocus(state);
        });
      }

      const full = state.node.querySelector('[data-map-fullscreen]');
      if (full) {
        full.addEventListener('click', () => {
          const on = state.node.classList.toggle('map--full');
          full.setAttribute('aria-pressed', String(on));
          document.body.style.overflow = on ? 'hidden' : '';
          setTimeout(() => state.map.invalidateSize(), 60);
        });
        document.addEventListener('keydown', (e) => {
          if (e.key === 'Escape' && state.node.classList.contains('map--full')) {
            full.click();
          }
        });
      }
    },

  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => LiveMap.init());
  } else {
    LiveMap.init();
  }
})();
