/* ==========================================================================
   chart.js -- the dashboard's "Today's Waste Type" card

   Chart.js by CDN. The chart is an enhancement: the same figures are printed
   as a definition list beside it, so the card is readable to a screen reader,
   in print, and if the CDN is unreachable.

   Sacks and kilos are charted as two separate series and never summed --
   they are different units, and one bar adding them would be a number that
   means nothing.
   ========================================================================== */

(function () {
  'use strict';

  const canvas = document.getElementById('wasteChart');
  if (!canvas || typeof Chart === 'undefined') return;

  let lines = [];
  try {
    lines = JSON.parse(canvas.dataset.wasteChart || '[]');
  } catch (err) {
    return;
  }
  if (!lines.length) return;

  // Read the theme's own colours rather than hardcoding hex, so the chart
  // follows the light/dark switch like everything else.
  const css = getComputedStyle(document.documentElement);
  const pick = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;

  const text = pick('--text-muted', '#64748b');
  const grid = pick('--border-subtle', '#e2e8f0');

  const labels = [...new Set(lines.map((l) => l.label))];
  const forUnit = (unit) => labels.map((label) => {
    const row = lines.find((l) => l.label === label && l.unit === unit);
    return row ? row.value : 0;
  });

  const datasets = [];
  const sacks = forUnit('Sack');
  const kilos = forUnit('Kilo');
  if (sacks.some((v) => v)) {
    datasets.push({ label: 'Sacks', data: sacks, backgroundColor: '#16a34a', borderRadius: 4 });
  }
  if (kilos.some((v) => v)) {
    datasets.push({ label: 'Kilos', data: kilos, backgroundColor: '#2f7fd0', borderRadius: 4 });
  }

  new Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: text, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const unit = ctx.dataset.label === 'Kilos' ? 'kg' : 'sacks';
              return `${ctx.parsed.y} ${unit}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: text,
            // Waste-type names are long; wrapping keeps the axis readable
            // without turning the labels sideways.
            callback(value) {
              const label = this.getLabelForValue(value);
              return label.length > 18 ? label.match(/.{1,18}(\s|$)/g) : label;
            },
          },
          grid: { display: false },
        },
        y: {
          beginAtZero: true,
          ticks: { color: text, precision: 0 },
          grid: { color: grid },
        },
      },
    },
  });
})();
