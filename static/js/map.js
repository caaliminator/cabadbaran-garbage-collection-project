/* ==========================================================================
   map.js -- the live tracking map

   Leaflet, loaded by CDN. Every coordinate, boundary, and colour comes from
   the server at runtime:

       /api/geo/config      centre, zoom, tiles, the four zone colour groups
       /api/geo/barangays   barangay boundary polygons
       /api/geo/mrfs        MRF markers
       /api/geo/hotspots    the optional hotspot overlay
       /api/live/vehicles   on-duty tricycles and trucks

   There is deliberately no coordinate in this file. Dropping the real
   barangay boundaries into data/geo/ is meant to require no code change at
   all, and a hardcoded fallback here would quietly defeat that.

   Layer order matters: zones sit underneath, hotspots above them, live
   vehicles on top -- so switching the hotspot layer on never obscures a
   moving truck.
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

      const zoneLayer = L.layerGroup().addTo(map);
      const mrfLayer = L.layerGroup().addTo(map);
      const hotspotLayer = L.layerGroup();
      const vehicleLayer = L.layerGroup().addTo(map);

      const state = {
        node, map, zoneLayer, mrfLayer, hotspotLayer, vehicleLayer,
        config,
        filter: node.dataset.mapFilter || 'all',
        barangay: node.dataset.mapBarangay || '',
        scope: node.dataset.mapScope || '',
        locateZoom: Number(node.dataset.mapLocate) || 0,
        followVehicle: node.dataset.mapFollow || '',
        markers: {},
      };

      // A drag or a pinch means the viewer has taken over. Geolocation is
      // slow -- a cold GPS fix can take ten seconds -- and yanking the map
      // out from under someone who is already reading it is worse than not
      // centring on them at all.
      state.map.on('dragstart', () => { state.userMoved = true; });

      await this.drawZones(state);
      await this.drawMrfs(state);

      // One listener for everything that changes with scale: the MRF labels,
      // and how much of the street layer the zone fills are allowed to cover.
      const onZoom = () => {
        this.refreshMrfLabels(state);
        this.refreshZoomBand(state);
      };
      onZoom();
      map.on('zoomend', onZoom);

      if (state.followVehicle) this.followMe(state);
      else if (state.locateZoom) this.locate(state);

      const control = L.control.layers(baseLayers, {
        'Barangay zones': zoneLayer,
        'MRF locations': mrfLayer,
        'Live vehicles': vehicleLayer,
      }, { collapsed: true }).addTo(map);

      if (config.hotspot_layer_enabled) {
        control.addOverlay(hotspotLayer, 'Hotspots');
        await this.drawHotspots(state);
      }

      L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);

      this.drawZoneLegend(state);
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

    async drawZones(state) {
      const data = await fetch('/api/geo/barangays').then((r) => r.json());
      const groups = {};
      (state.config.zone_groups || []).forEach((g, i) => { groups[g.key] = i; });

      const drawn = L.geoJSON(data, {
        // Two classes, no colour: the zone group and the barangay id. Which of
        // them actually paints the polygon is decided in components.css, so
        // this file still holds no colour value of its own.
        style: (feature) => ({
          className: 'map-zone'
            + ` map-zone--${feature.properties.zone_group || 'none'}`
            + (feature.properties.barangay_id
                ? ` map-zone--${feature.properties.barangay_id}` : ''),
          weight: 2,
        }),
        onEachFeature: (feature, layer) => {
          const p = feature.properties;
          layer.bindTooltip(p.name, { permanent: false, direction: 'center' });
          layer.bindPopup(
            `<strong>${p.name}</strong><br>${p.zone_label || ''}` +
            (p.purok_count ? `<br>${p.purok_count} puroks` : '')
          );
        },
      });

      state.zoneLayer.clearLayers();
      state.zoneLayers = {};
      drawn.eachLayer((layer) => {
        const id = layer.feature?.properties?.barangay_id;
        if (id) state.zoneLayers[id] = layer;
      });

      if (data.meta.with_geometry > 0) {
        drawn.addTo(state.zoneLayer);
        state.cityBounds = drawn.getBounds();

        // A page pinned to one barangay opens on that barangay, with the rest
        // of the city faded back rather than hidden -- a collector needs to see
        // where their zone sits, not a shape floating on grey.
        if (state.barangay && state.zoneLayers[state.barangay]) {
          this.focusBarangay(state, state.barangay);
        } else if (state.cityBounds.isValid()) {
          state.map.fitBounds(state.cityBounds, { padding: [16, 16] });
        }

        // Geometry that is still flagged a placeholder draws normally, but the
        // map must never present invented shapes as surveyed boundaries.
        if (data.meta.placeholder) {
          this.note(state,
            'Boundaries and MRF pins are illustrative approximations, not ' +
            'surveyed data. See docs/DATA_REQUIREMENTS.md.');
        }
      } else {
        // Nothing to draw is a normal state before the real boundary data
        // arrives -- say so rather than showing a blank map.
        this.note(state,
          `Barangay boundaries have not been loaded yet (${data.meta.total} barangays known). ` +
          'See docs/DATA_REQUIREMENTS.md.');
      }
    },

    async drawMrfs(state) {
      const data = await fetch('/api/geo/mrfs').then((r) => r.json());
      state.mrfLayer.clearLayers();

      // 31 MRFs inside a 17 km city overlap badly at city zoom, so these are
      // small dots that carry their name in a tooltip, not labelled pills like
      // the vehicles. The label only appears once the map is zoomed in far
      // enough for the dots to have separated (see refreshMrfLabels).
      state.mrfMarkers = [];
      data.mrfs.filter((m) => m.located).forEach((mrf) => {
        const marker = L.marker([mrf.lat, mrf.lng], {
          icon: L.divIcon({
            className: 'map-mrf'
              + (state.barangay && mrf.barangay_id === state.barangay
                  ? ' map-mrf--focus' : ''),
            html: '<span class="map-mrf__dot"></span>'
                  + `<span class="map-mrf__name">${mrf.barangay_name}</span>`,
            iconSize: [14, 14],
            iconAnchor: [7, 7],
          }),
          // Keep the focused barangay's own MRF clickable above its neighbours.
          zIndexOffset: state.barangay && mrf.barangay_id === state.barangay ? 400 : 0,
        })
          .bindTooltip(mrf.name, { direction: 'top', offset: [0, -8] })
          .bindPopup(
            `<strong>${mrf.name}</strong><br>Materials Recovery Facility` +
            `<br>Barangay ${mrf.number} — ${mrf.barangay_name}`)
          .addTo(state.mrfLayer);
        state.mrfMarkers.push(marker);
      });

      this.refreshMrfLabels(state);

      if (data.meta.located === 0 && data.meta.total > 0) {
        const existing = state.node.querySelector('[data-map-note]')?.textContent || '';
        this.note(state, `${existing} MRF locations are not loaded yet.`.trim());
      }
    },

    /* MRF names are only legible once the dots have room; below that zoom
       they would stack into an unreadable block, which is exactly what the
       first version of this map did. */
    refreshMrfLabels(state) {
      const show = state.map.getZoom() >= 14;
      (state.mrfMarkers || []).forEach((marker) => {
        const node = marker.getElement();
        if (node) node.classList.toggle('map-mrf--labelled', show);
      });
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
      if (!navigator.geolocation) {
        this.note(state, 'This device cannot report its location.');
        return;
      }

      let marker = null;
      let halo = null;
      let centred = false;

      navigator.geolocation.watchPosition(
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
          if (!centred) {
            state.map.setView(point, state.locateZoom || 16);
            centred = true;
          } else if (!state.userMoved) {
            state.map.panTo(point);
          }
        },
        (error) => {
          this.note(state, error && error.code === 1
            ? 'Location is blocked for this site. The map cannot follow you '
              + 'until you allow it in your browser settings.'
            : 'Your location is unavailable right now.');
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 });
    },

    /* Which scale the map is being read at, published to CSS as a data
       attribute on the map container.

       Zone fills are a city-scale device: 31 coloured sheets are what makes
       the city legible from above, and the same sheets are what hide the
       roads once you are down among them. Rather than switching the layer
       off, the fills step back as the scale closes in -- the barangay stays
       identifiable, and its streets come through underneath. The thresholds
       are named rather than numeric so the CSS reads as intent. */
    refreshZoomBand(state) {
      const zoom = state.map.getZoom();
      state.node.dataset.mapZoom =
        zoom >= 17 ? 'street' : zoom >= 15 ? 'near' : 'city';
    },

    /* Zoom to one barangay and fade the others back. */
    focusBarangay(state, barangayId) {
      const target = (state.zoneLayers || {})[barangayId];
      Object.entries(state.zoneLayers || {}).forEach(([id, layer]) => {
        const node = layer.getElement && layer.getElement();
        if (node) node.classList.toggle('map-zone--faded', id !== barangayId);
      });
      if (target && target.getBounds) {
        state.map.fitBounds(target.getBounds(), { padding: [40, 40], maxZoom: 15 });
      }
    },

    clearFocus(state) {
      Object.values(state.zoneLayers || {}).forEach((layer) => {
        const node = layer.getElement && layer.getElement();
        if (node) node.classList.remove('map-zone--faded');
      });
      if (state.cityBounds && state.cityBounds.isValid()) {
        state.map.fitBounds(state.cityBounds, { padding: [16, 16] });
      }
    },

    /* The four zone colour groups, printed from the server's own config so the
       legend can never drift from what the polygons are painted with. */
    drawZoneLegend(state) {
      const slot = state.node.querySelector('[data-map-zones]');
      if (!slot) return;
      slot.innerHTML = '';
      (state.config.zone_groups || []).forEach((group) => {
        const item = el('span', 'map__legend-item');
        item.appendChild(el('span', `map__legend-dot map__legend-dot--${group.key}`));
        item.appendChild(el('span', null, group.label));
        slot.appendChild(item);
      });
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
        if (state.followVehicle && v.vehicle === state.followVehicle
            && !state.userMoved) {
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

    icon(kind, label) {
      return L.divIcon({
        className: `map-pin map-pin--${kind}`,
        html: `<span class="map-pin__label">${label}</span>`,
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
