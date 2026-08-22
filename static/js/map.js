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

      L.tileLayer(config.tile_url, {
        attribution: config.attribution,
        maxZoom: 19,
      }).addTo(map);

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
      };

      await this.drawZones(state);
      await this.drawMrfs(state);

      const control = L.control.layers(null, {
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
      await this.drawVehicles(state);
      setInterval(() => this.drawVehicles(state), REFRESH_MS);
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
      state.map.off('zoomend', state.onZoom);
      state.onZoom = () => this.refreshMrfLabels(state);
      state.map.on('zoomend', state.onZoom);

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
        item.appendChild(el('span', 'text-2xs', group.label));
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

      state.vehicleLayer.clearLayers();
      data.vehicles.forEach((v) => {
        L.marker([v.lat, v.lng], { icon: this.icon(v.kind, v.vehicle) })
          .bindPopup(
            `<strong>${v.vehicle}</strong>` +
            (v.name ? `<br>${v.name}` : '') +
            (v.barangays && v.barangays.length ? `<br>${v.barangays.join(', ')}` : '')
          )
          .addTo(state.vehicleLayer);
      });

      this.updateLegend(state, data);
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
    },

    /* ---- Controls ------------------------------------------------------ */

    wireControls(state) {
      state.node.querySelectorAll('[data-map-tab]').forEach((btn) => {
        btn.addEventListener('click', () => {
          state.node.querySelectorAll('[data-map-tab]').forEach((b) => {
            b.setAttribute('aria-selected', String(b === btn));
          });
          state.filter = btn.dataset.mapTab;
          this.drawVehicles(state);
        });
      });

      const select = state.node.querySelector('[data-map-barangay]');
      if (select) {
        select.addEventListener('change', () => {
          state.barangay = select.value;
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
