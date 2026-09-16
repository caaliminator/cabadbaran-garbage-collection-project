/* ==========================================================================
   app.js -- application shell behaviour
   Vanilla ES2020. No frameworks, no build step.
   Modules: theme, nav drawer, popovers, modals, toasts, clock, forms.
   ========================================================================== */

(function () {
  'use strict';

  const root = document.documentElement;
  const $  = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  /* ======================================================================
     THEME -- persisted light/dark, defaults to the OS preference
     ====================================================================== */

  const Theme = {
    KEY: 'gcts-theme',

    init() {
      const saved = localStorage.getItem(this.KEY);
      if (saved === 'light' || saved === 'dark') root.dataset.theme = saved;

      $$('[data-theme-toggle]').forEach((btn) => {
        btn.addEventListener('click', () => this.toggle(btn));
        this.syncLabel(btn);
      });
    },

    current() {
      return root.dataset.theme
        || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    },

    toggle(btn) {
      const next = this.current() === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      localStorage.setItem(this.KEY, next);
      this.syncLabel(btn);
      $$('[data-theme-toggle]').forEach((b) => this.syncLabel(b));
    },

    syncLabel(btn) {
      const isDark = this.current() === 'dark';
      btn.setAttribute('aria-pressed', String(isDark));
      btn.setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
      btn.title = btn.getAttribute('aria-label');
    },
  };

  /* ======================================================================
     NAV DRAWER -- off-canvas sidebar below 1024px
     Focus is trapped while open and restored on close.
     ====================================================================== */

  const Drawer = {
    rail: null,
    scrim: null,
    toggles: [],
    lastFocus: null,

    init() {
      this.rail  = $('#rail');
      this.scrim = $('#scrim');
      this.toggles = $$('[data-nav-toggle]');
      if (!this.rail) return;

      this.toggles.forEach((btn) =>
        btn.addEventListener('click', () => this.toggle()));

      if (this.scrim) this.scrim.addEventListener('click', () => this.close());

      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.isOpen()) this.close();
        if (e.key === 'Tab' && this.isOpen()) this.trap(e);
      });

      // Navigating on a phone should dismiss the drawer.
      $$('.rail__link', this.rail).forEach((link) =>
        link.addEventListener('click', () => {
          if (matchMedia('(max-width: 1024px)').matches) this.close();
        }));

      // Returning to desktop width must clear the drawer state.
      matchMedia('(min-width: 1025px)').addEventListener('change', (e) => {
        if (e.matches) this.close();
      });

      this.sync();
    },

    isOpen() { return root.dataset.navOpen === 'true'; },

    toggle() { this.isOpen() ? this.close() : this.open(); },

    open() {
      this.lastFocus = document.activeElement;
      root.dataset.navOpen = 'true';
      this.sync();
      const first = $('.rail__link', this.rail);
      if (first) first.focus();
    },

    close() {
      root.dataset.navOpen = 'false';
      this.sync();
      if (this.lastFocus && matchMedia('(max-width: 1024px)').matches) {
        this.lastFocus.focus();
        this.lastFocus = null;
      }
    },

    sync() {
      const open = this.isOpen();
      this.toggles.forEach((b) => b.setAttribute('aria-expanded', String(open)));
      if (this.scrim) this.scrim.hidden = !open;
    },

    trap(e) {
      const items = $$('a[href], button:not([disabled])', this.rail)
        .filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last  = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    },
  };

  /* ======================================================================
     POPOVERS -- notification bell, user menu
     ====================================================================== */

  const Popover = {
    init() {
      $$('[data-pop-trigger]').forEach((trigger) => {
        const panel = document.getElementById(trigger.getAttribute('aria-controls'));
        if (!panel) return;

        trigger.addEventListener('click', (e) => {
          e.stopPropagation();
          const open = panel.dataset.open === 'true';
          this.closeAll();
          if (!open) this.open(trigger, panel);
        });

        panel.addEventListener('click', (e) => e.stopPropagation());
      });

      document.addEventListener('click', () => this.closeAll());
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') this.closeAll();
      });
    },

    open(trigger, panel) {
      panel.dataset.open = 'true';
      trigger.setAttribute('aria-expanded', 'true');
    },

    closeAll() {
      $$('[data-pop-trigger]').forEach((t) => t.setAttribute('aria-expanded', 'false'));
      $$('.pop__panel').forEach((p) => (p.dataset.open = 'false'));
    },
  };

  /* ======================================================================
     MODALS -- confirm dialogs and detail sheets
     ====================================================================== */

  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  const Modal = {
    lastFocus: null,

    init() {
      $$('[data-modal-open]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const modal = document.getElementById(btn.dataset.modalOpen);
          if (!modal) return;
          // Let a trigger seed the dialog's fields: data-set-<field>="value".
          // [data-fill] receives display text; [data-fill-value] receives a
          // form control's value, so one trigger can drive a read-only detail
          // dialog and a prefilled edit form with the same attributes.
          Object.entries(btn.dataset).forEach(([key, value]) => {
            if (!key.startsWith('set')) return;
            const name = key.slice(3).toLowerCase();

            const text = modal.querySelector(`[data-fill="${name}"]`);
            if (text) {
              if ('value' in text && text.tagName !== 'DIV') text.value = value;
              else text.textContent = value;
            }

            modal.querySelectorAll(`[data-fill-value="${name}"]`).forEach((input) => {
              // An assign dropdown disables whoever is already taken. The row
              // being edited holds one of those, and it is legitimately
              // selectable *for this row* -- so re-enable its own option
              // before selecting it, or the dialog opens showing a value the
              // user cannot pick again after changing their mind.
              if (input.tagName === 'SELECT') {
                const own = [...input.options].find((o) => o.value === value);
                if (own) own.disabled = false;
              }
              input.value = value;
              // Selects rebuilt by RoleFields need the change event to re-run.
              input.dispatchEvent(new Event('change', { bubbles: true }));
            });
          });

          // Rows that only belong to one kind of record: data-show-when
          // ="field:value" shows the row when the trigger set that field to
          // that value, and hides it otherwise. An empty value means "only
          // when the field was not set" -- which is how one MRF dialog shows
          // either a single Assigned Truck or the original/current pair,
          // without needing two dialogs that would then have to be kept in
          // step with each other.
          const seeded = (field) =>
            btn.dataset['set' + field.charAt(0).toUpperCase() + field.slice(1)] || '';

          modal.querySelectorAll('[data-show-when]').forEach((node) => {
            const [field, want = ''] = node.dataset.showWhen.split(':');
            node.hidden = seeded(field) !== want;
          });

          // The open-ended version: show the row whenever the field has
          // anything in it at all. A note that only exists sometimes -- what a
          // carry-over is still waiting for -- would otherwise leave an empty
          // line behind on every record that is not waiting for anything.
          modal.querySelectorAll('[data-show-when-set]').forEach((node) => {
            node.hidden = !seeded(node.dataset.showWhenSet);
          });

          // Role-dependent fields must reflect the seeded role, not the last
          // state the dialog was left in.
          modal.querySelectorAll('[data-user-form]').forEach((f) => RoleFields.sync(f));
          if (modal.matches('[data-user-form]')) RoleFields.sync(modal);

          // Anything else that derives from the seeded values -- the
          // "Replacement Until" field keys off the Status that was just set.
          modal.querySelectorAll('form').forEach((f) =>
            f.dispatchEvent(new CustomEvent('gcts:filled')));
          if (modal.tagName === 'FORM') {
            modal.dispatchEvent(new CustomEvent('gcts:filled'));
          }

          // An image proof only exists on some entries, so its block is
          // hidden rather than left showing a broken image.
          const proofWrap = modal.querySelector('[data-proof-wrap]');
          if (proofWrap) {
            const url = btn.dataset.setProof || '';
            const img = modal.querySelector('[data-proof-img]');
            proofWrap.hidden = !url;
            if (img) img.src = url || '';
          }

          // Checkbox groups run last, after any dependent control has been
          // rebuilt by the handlers above.
          modal.querySelectorAll('[data-fill-checks]').forEach((box) => {
            const wanted = (btn.dataset[`set${cap(box.dataset.fillChecks)}`] || '')
              .split(',').map((s) => s.trim()).filter(Boolean);
            box.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
              cb.checked = wanted.includes(cb.value);
            });
          });

          this.open(modal);
        });
      });

      $$('[data-modal-close]').forEach((btn) => {
        btn.addEventListener('click', () => this.close(btn.closest('.modal')));
      });

      $$('.modal').forEach((modal) => {
        modal.addEventListener('click', (e) => {
          if (e.target === modal) this.close(modal);
        });
      });

      document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        // A photo proof is opened from inside this dialog and sits on top of
        // it. One press closes the photo and nothing else -- the admin is
        // still reading the entry underneath. Checked here rather than
        // stopped from the lightbox's own handler, because this listener is
        // registered first and stopImmediatePropagation cannot reach back.
        if (Lightbox.isOpen()) return;
        const open = $('.modal[data-open="true"]');
        if (open) this.close(open);
      });
    },

    open(modal) {
      this.lastFocus = document.activeElement;
      modal.dataset.open = 'true';
      modal.removeAttribute('aria-hidden');
      root.style.overflow = 'hidden';
      const focusable = modal.querySelector(
        'input:not([type="hidden"]), select, textarea, button, [href]');
      if (focusable) setTimeout(() => focusable.focus(), 60);
    },

    close(modal) {
      if (!modal) return;
      modal.dataset.open = 'false';
      modal.setAttribute('aria-hidden', 'true');
      root.style.overflow = '';
      if (this.lastFocus) { this.lastFocus.focus(); this.lastFocus = null; }
    },
  };

  /* ======================================================================
     LIGHTBOX -- click a photo proof to see it full screen, click again to
     zoom to actual size.

     Every trigger is a real <a href> to the image, so with scripting off the
     photo still opens in the browser's own viewer. This intercepts the click
     and shows it in place instead, which matters most inside the View Details
     dialog: following the link there throws away the dialog and the row the
     admin was looking at.

     Triggers are found by delegation rather than bound up front, because the
     proof <img> in the entry dialog has its src swapped in each time a row is
     opened, and a proof link can sit in a table a filter has just redrawn.
     ====================================================================== */

  const Lightbox = {
    box: null,

    init() {
      this.box = $('[data-lightbox]');
      if (!this.box) return;

      this.img      = $('[data-lightbox-img]', this.box);
      this.caption  = $('[data-lightbox-caption]', this.box);
      this.stage    = $('[data-lightbox-stage]', this.box);
      this.download = $('[data-lightbox-download]', this.box);
      this.zoomBtn  = $('[data-lightbox-zoom]', this.box);
      this.zoomText = $('[data-lightbox-zoom-label]', this.box);
      this.hint     = $('[data-lightbox-hint]', this.box);

      document.addEventListener('click', (e) => {
        const trigger = e.target.closest('[data-zoomable], [data-zoom-src]');
        if (!trigger) return;
        const src = trigger.dataset.zoomSrc
                 || trigger.getAttribute('href')
                 || trigger.currentSrc
                 || trigger.src;
        if (!src) return;
        e.preventDefault();
        this.open(src, this.captionFor(trigger));
      });

      // An <img> is not focusable and takes no Enter key, so a zoomable one is
      // given a button's keyboard behaviour to match the pointer's.
      document.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const el = document.activeElement;
        if (!el || el.tagName !== 'IMG' || !el.matches('[data-zoomable]')) return;
        e.preventDefault();
        this.open(el.currentSrc || el.src, this.captionFor(el));
      });

      // Whether zooming can do anything depends on the photo: one smaller than
      // the stage is already at 100%, and "Actual size" would change nothing.
      // Only measurable once the file has decoded.
      this.img.addEventListener('load', () => this.measure());

      this.img.addEventListener('click', () => {
        if (this.canZoom) this.toggleZoom();
      });
      this.zoomBtn.addEventListener('click', () => this.toggleZoom());

      $('[data-lightbox-close]', this.box)
        .addEventListener('click', () => this.close());

      // Backdrop only: a click that lands on the image, the bar or a button
      // has already been handled by whatever it landed on.
      this.box.addEventListener('click', (e) => {
        if (e.target === this.box || e.target === this.stage) this.close();
      });

      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.isOpen()) this.close();
      });
    },

    isOpen() { return this.box && this.box.dataset.open === 'true'; },

    captionFor(trigger) {
      return trigger.dataset.zoomCaption
          || trigger.getAttribute('alt')
          || trigger.textContent.trim()
          || 'Image proof';
    },

    open(src, caption) {
      this.lastFocus = document.activeElement;
      this.img.src = src;
      this.img.alt = caption;
      this.caption.textContent = caption;
      this.download.href = src;
      // Assume zoomable until the loaded photo says otherwise; `measure()`
      // corrects it, and a stale answer from the previous photo would
      // disable the control on one that needs it.
      this.canZoom = true;
      this.setZoom('fit');

      // Restored on close rather than blanked: the dialog underneath may have
      // locked scrolling, and closing the photo must not unlock it.
      this.prevOverflow = root.style.overflow;
      root.style.overflow = 'hidden';

      this.box.dataset.open = 'true';
      this.box.removeAttribute('aria-hidden');
      setTimeout(() => this.zoomBtn.focus(), 60);
    },

    close() {
      if (!this.isOpen()) return;
      this.box.dataset.open = 'false';
      this.box.setAttribute('aria-hidden', 'true');
      root.style.overflow = this.prevOverflow || '';
      // Dropped so a stale photo is never briefly visible behind the fade the
      // next time this opens.
      this.img.removeAttribute('src');
      if (this.lastFocus && this.lastFocus.focus) this.lastFocus.focus();
      this.lastFocus = null;
    },

    measure() {
      // Fit never enlarges (max-width/height only shrink), so the photo is
      // already at full size whenever it is not being scaled down.
      const shrunk = this.img.naturalWidth > this.img.clientWidth + 1
                  || this.img.naturalHeight > this.img.clientHeight + 1;
      this.canZoom = this.box.dataset.zoom === 'actual' || shrunk;

      this.zoomBtn.disabled = !this.canZoom;
      this.img.style.cursor = this.canZoom ? '' : 'default';
      if (!this.canZoom && this.hint) {
        this.hint.textContent =
          `Shown at full size, ${this.img.naturalWidth}×${this.img.naturalHeight} · Esc to close`;
      }
    },

    toggleZoom() {
      this.setZoom(this.box.dataset.zoom === 'actual' ? 'fit' : 'actual');
    },

    setZoom(mode) {
      this.box.dataset.zoom = mode;
      const actual = mode === 'actual';
      this.zoomBtn.setAttribute('aria-pressed', String(actual));
      if (this.zoomText) this.zoomText.textContent = actual ? 'Fit to screen' : 'Actual size';
      this.zoomBtn.setAttribute('aria-label',
        actual ? 'Fit the image to the screen' : 'Show the image at actual size');
      if (this.hint && this.canZoom !== false) {
        this.hint.textContent = actual
          ? 'Scroll or drag to move around · Esc to close'
          : 'Click the image to zoom · Esc to close';
      }
      if (actual) {
        // Open on the middle of the photo rather than its top-left corner.
        this.stage.scrollLeft = (this.stage.scrollWidth - this.stage.clientWidth) / 2;
        this.stage.scrollTop = (this.stage.scrollHeight - this.stage.clientHeight) / 2;
      }
    },
  };

  /* ======================================================================
     TOASTS -- flash messages, auto-dismissed, announced politely
     ====================================================================== */

  const Toast = {
    init() {
      $$('.toast').forEach((t) => this.schedule(t));
      $$('.toast__close').forEach((btn) =>
        btn.addEventListener('click', () => this.dismiss(btn.closest('.toast'))));
    },

    schedule(toast) {
      const ms = Number(toast.dataset.timeout || 5200);
      let timer = setTimeout(() => this.dismiss(toast), ms);
      // Pause the countdown while the user is reading or interacting.
      toast.addEventListener('mouseenter', () => clearTimeout(timer));
      toast.addEventListener('focusin', () => clearTimeout(timer));
      toast.addEventListener('mouseleave', () => {
        timer = setTimeout(() => this.dismiss(toast), 2000);
      });
    },

    dismiss(toast) {
      if (!toast || toast.dataset.leaving === 'true') return;
      toast.dataset.leaving = 'true';
      toast.addEventListener('animationend', () => toast.remove(), { once: true });
      setTimeout(() => toast.remove(), 400); // fallback if animations are off
    },
  };

  /* ======================================================================
     CLOCK -- keeps the topbar time honest without a page refresh
     ====================================================================== */

  const Clock = {
    init() {
      const el = $('[data-clock]');
      if (!el) return;
      const tick = () => {
        el.textContent = new Date()
          .toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      };
      tick();
      setInterval(tick, 30000);
    },
  };

  /* ======================================================================
     FORMS -- password reveal, client-side validation, confirm-on-submit
     ====================================================================== */

  const Forms = {
    init() {
      this.passwordToggles();
      this.validation();
      this.confirmations();
    },

    passwordToggles() {
      $$('[data-reveal]').forEach((btn) => {
        // Each button ships both icons; show "eye" and hide "eye-off" initially.
        const icons = $$('svg', btn);
        icons.forEach((svg, i) => (svg.style.display = i === 0 ? 'block' : 'none'));

        btn.addEventListener('click', () => {
          const input = document.getElementById(btn.dataset.reveal);
          if (!input) return;
          const show = input.type === 'password';
          input.type = show ? 'text' : 'password';
          btn.setAttribute('aria-pressed', String(show));
          btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
          $$('svg', btn).forEach((svg, i) => (svg.style.display = (i === 0) === show ? 'none' : 'block'));
        });
      });
    },

    /* Progressive enhancement over native constraint validation:
       we let the browser do the checking, then render the message inline. */
    validation() {
      $$('form[data-validate]').forEach((form) => {
        form.setAttribute('novalidate', '');

        form.addEventListener('submit', (e) => {
          let firstBad = null;
          $$('[required], [pattern], [type="email"]', form).forEach((input) => {
            if (!input.checkValidity()) {
              this.showError(input, input.validationMessage);
              firstBad = firstBad || input;
            } else {
              this.clearError(input);
            }
          });

          // Cross-field rule: password confirmation.
          const pw = $('[data-password]', form);
          const cf = $('[data-confirm]', form);
          if (pw && cf && pw.value !== cf.value) {
            this.showError(cf, 'Passwords do not match.');
            firstBad = firstBad || cf;
          }

          if (firstBad) {
            e.preventDefault();
            firstBad.focus();
            firstBad.scrollIntoView({ block: 'center', behavior: 'smooth' });
          }
        });

        // Clear the error as soon as the field becomes valid again.
        $$('input, select, textarea', form).forEach((input) => {
          input.addEventListener('input', () => {
            if (input.checkValidity()) this.clearError(input);
          });
        });
      });
    },

    showError(input, message) {
      input.setAttribute('aria-invalid', 'true');
      const field = input.closest('.field');
      if (!field) return;
      let err = $('.field__error', field);
      if (!err) {
        err = document.createElement('p');
        err.className = 'field__error';
        err.setAttribute('role', 'alert');
        err.id = `${input.id || input.name}-error`;
        field.appendChild(err);
      }
      err.textContent = message;
      input.setAttribute('aria-describedby', err.id);
    },

    clearError(input) {
      input.removeAttribute('aria-invalid');
      const field = input.closest('.field');
      const err = field && $('.field__error', field);
      if (err) err.remove();
    },

    /* Destructive actions ask first. */
    confirmations() {
      $$('form[data-confirm-message]').forEach((form) => {
        form.addEventListener('submit', (e) => {
          if (!window.confirm(form.dataset.confirmMessage)) e.preventDefault();
        });
      });
    },
  };

  /* ======================================================================
     SEGMENTED CONTROLS -- pure client-side view switches
     ====================================================================== */

  const Segmented = {
    init() {
      $$('[data-segmented]').forEach((group) => {
        const btns = $$('.segmented__btn', group);
        btns.forEach((btn) => {
          btn.addEventListener('click', () => {
            btns.forEach((b) => b.setAttribute('aria-selected', String(b === btn)));
            group.dispatchEvent(new CustomEvent('segment:change', {
              detail: { value: btn.dataset.value },
              bubbles: true,
            }));
          });
        });

        // Arrow-key navigation, per the WAI-ARIA tabs pattern.
        group.addEventListener('keydown', (e) => {
          const i = btns.indexOf(document.activeElement);
          if (i < 0) return;
          let next = null;
          if (e.key === 'ArrowRight') next = btns[(i + 1) % btns.length];
          if (e.key === 'ArrowLeft')  next = btns[(i - 1 + btns.length) % btns.length];
          if (next) { e.preventDefault(); next.focus(); next.click(); }
        });
      });
    },
  };

  /* ======================================================================
     REVEAL -- radio groups that show/hide dependent panels

     <fieldset data-reveal-group="record"> ...radios... </fieldset>
     <section data-reveal-group="record" data-reveal-when="Collected"> …

     Hidden panels have their fields disabled so a `required` control inside
     one can never block submission of the visible branch.
     ====================================================================== */

  const Reveal = {
    init() {
      const groups = new Set(
        $$('[data-reveal-group]').map((el) => el.dataset.revealGroup));

      groups.forEach((name) => {
        const radios = $$(`[data-reveal-group="${name}"] input[type="radio"]`);
        const panels = $$(`[data-reveal-group="${name}"][data-reveal-when]`);
        if (!radios.length || !panels.length) return;

        const sync = () => {
          const picked = radios.find((r) => r.checked);
          const value = picked ? picked.value : null;
          panels.forEach((panel) => {
            const show = panel.dataset.revealWhen === value;
            panel.hidden = !show;
            $$('input, select, textarea', panel).forEach((el) => {
              el.disabled = !show;
            });
          });
        };

        radios.forEach((r) => r.addEventListener('change', sync));
        sync();
      });
    },
  };

  /* ======================================================================
     STEPPER -- +/- quantity controls
     ====================================================================== */

  const Stepper = {
    init() {
      $$('.stepper').forEach((stepper) => {
        const input = $('.stepper__input', stepper);
        if (!input) return;

        $$('[data-step]', stepper).forEach((btn) => {
          btn.addEventListener('click', () => {
            const delta = Number(btn.dataset.step);
            const min = input.min !== '' ? Number(input.min) : -Infinity;
            const max = input.max !== '' ? Number(input.max) : Infinity;
            const next = Math.min(max, Math.max(min, (Number(input.value) || 0) + delta));
            input.value = next;
            input.dispatchEvent(new Event('input', { bubbles: true }));
          });
        });

        // Never let a stray keystroke leave a non-numeric or negative value.
        input.addEventListener('blur', () => {
          const min = input.min !== '' ? Number(input.min) : 0;
          if (input.value === '' || Number.isNaN(Number(input.value))) input.value = min;
        });
      });
    },
  };

  /* ======================================================================
     GEO -- capture the collector's coordinates for a stop record
     ====================================================================== */

  const Geo = {
    init() {
      // Live timestamp on record screens.
      $$('[data-live-timestamp]').forEach((el) => {
        const tick = () => {
          el.textContent = new Date().toLocaleString([], {
            month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
          });
        };
        tick();
        setInterval(tick, 30000);
      });

      $$('[data-geo-capture]').forEach((btn) => {
        const scope = btn.closest('.card') || document;
        const output = $('[data-geo-output]', scope);
        const hidden = $('[data-geo-input]', scope);
        const note = $('[data-geo-status]', scope);

        btn.addEventListener('click', () => {
          if (!('geolocation' in navigator)) {
            if (note) note.textContent = 'This device does not support location capture.';
            return;
          }

          btn.disabled = true;
          if (note) note.textContent = 'Getting your location…';

          navigator.geolocation.getCurrentPosition(
            (pos) => {
              const { latitude, longitude, accuracy } = pos.coords;
              const value = `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
              if (output) output.textContent = value;
              if (hidden) hidden.value = value;
              if (note) note.textContent = `Captured — accurate to about ${Math.round(accuracy)} m.`;
              btn.disabled = false;
            },
            (err) => {
              const reasons = {
                1: 'Location permission was denied.',
                2: 'Location is unavailable right now.',
                3: 'Location request timed out.',
              };
              if (note) note.textContent = reasons[err.code] || 'Could not get your location.';
              btn.disabled = false;
            },
            { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
          );
        });
      });
    },
  };

  /* ======================================================================
     DROPZONE -- photo proof with an inline preview
     ====================================================================== */

  const Dropzone = {
    init() {
      $$('[data-dropzone]').forEach((zone) => {
        const input   = $('[data-dropzone-input]', zone);
        const label   = $('[data-dropzone-label]', zone);
        const preview = $('[data-dropzone-preview]', zone);
        if (!input) return;

        input.addEventListener('change', () => {
          const file = input.files && input.files[0];
          if (!file) return;

          if (file.size > 5 * 1024 * 1024) {
            if (label) label.textContent = 'That file is larger than 5 MB — choose a smaller photo.';
            input.value = '';
            return;
          }

          if (label) label.textContent = file.name;
          if (preview) {
            preview.src = URL.createObjectURL(file);
            preview.hidden = false;
            preview.addEventListener('load', () => URL.revokeObjectURL(preview.src), { once: true });
          }
        });
      });
    },
  };

  /* ======================================================================
     DUTY TRACKER

     While a collector is On Duty the phone streams its position to the
     server, which is what puts their marker on every live map. Off duty, the
     watcher stops -- someone who has gone home should not still appear to be
     working a route.

     Updates are throttled to one every few seconds (the server decides the
     interval): watchPosition can fire many times a second on a moving
     vehicle, and neither the map nor the battery benefits from that.

     Phase 7 moves this onto a socket; the POST stays as the fallback for
     when the socket is down.
     ====================================================================== */

  const Duty = {
    watchId: null,
    lastSent: 0,

    init() {
      const card = $('[data-duty-card]');
      if (!card || card.dataset.dutyState !== 'on') return;

      const status = $('[data-duty-status]', card);
      const endpoint = card.dataset.dutyEndpoint;
      const intervalMs = (Number(card.dataset.dutyInterval) || 8) * 1000;

      if (!('geolocation' in navigator)) {
        if (status) {
          status.textContent =
            'This device cannot share location, so you will not appear on the live map.';
        }
        return;
      }

      this.watchId = navigator.geolocation.watchPosition(
        (pos) => {
          const now = Date.now();
          if (now - this.lastSent < intervalMs) return;
          this.lastSent = now;

          const { latitude, longitude, accuracy } = pos.coords;
          fetch(endpoint, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '',
            },
            body: JSON.stringify({ lat: latitude, lng: longitude, accuracy }),
          })
            .then((r) => {
              if (status) {
                status.textContent = r.ok
                  ? `Sharing your location — accurate to about ${Math.round(accuracy)} m.`
                  : 'Location could not be sent. Retrying.';
              }
            })
            .catch(() => {
              // Offline in the field is normal; the next fix retries.
              if (status) status.textContent = 'Offline — location will resend when you reconnect.';
            });
        },
        (err) => {
          const reasons = {
            1: 'Location permission was denied, so you will not appear on the live map. Enable it in your browser settings.',
            2: 'Location is unavailable right now.',
            3: 'Location request timed out.',
          };
          if (status) status.textContent = reasons[err.code] || 'Could not get your location.';
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
      );

      window.addEventListener('pagehide', () => {
        if (this.watchId !== null) navigator.geolocation.clearWatch(this.watchId);
      });
    },
  };

  /* ======================================================================
     ROLE-DEPENDENT FORM FIELDS

     A barangay admin needs a barangay; a collector needs a barangay and a
     vehicle of the matching type; a city admin needs neither. Showing every
     field to everyone invites the wrong data, so the role select drives which
     fields are visible and required, and narrows the vehicle list to the
     right type.

     This is convenience only. user_service._validate applies the same rules
     server-side, because a form post can be crafted by hand.
     ====================================================================== */

  const RoleFields = {
    NEEDS_BARANGAY: ['barangay_admin', 'tricycle_collector'],
    NEEDS_VEHICLE: ['tricycle_collector', 'truck_collector'],

    init() {
      $$('[data-user-form]').forEach((form) => {
        const role = form.querySelector('select[name="role"]');
        if (!role) return;
        role.addEventListener('change', () => this.sync(form));
        this.sync(form);
      });
    },

    sync(form) {
      const role = form.querySelector('select[name="role"]');
      if (!role) return;
      const value = role.value;

      this.toggle(form, 'barangay', this.NEEDS_BARANGAY.includes(value));
      this.toggle(form, 'vehicle', this.NEEDS_VEHICLE.includes(value));

      // Only offer units of the type this role can drive.
      form.querySelectorAll('optgroup[data-vehicle-group]').forEach((group) => {
        const matches = group.dataset.vehicleGroup === value;
        group.hidden = !matches;
        group.disabled = !matches;
      });
      const vehicle = form.querySelector('select[name="assigned_vehicle"]');
      if (vehicle && vehicle.selectedOptions.length) {
        const chosen = vehicle.selectedOptions[0];
        const group = chosen.closest('optgroup');
        if (group && group.disabled) vehicle.value = '';
      }
    },

    toggle(form, name, show) {
      form.querySelectorAll(`[data-role-field="${name}"]`).forEach((wrap) => {
        wrap.hidden = !show;
        wrap.querySelectorAll('select, input').forEach((input) => {
          input.required = show;
          input.disabled = !show;   // keeps a hidden field out of the payload
        });
      });
    },
  };

  /* ======================================================================
     PUBLIC REPORT FORM

     Barangay -> Purok -> property. The form works fully without this: each
     step is a normal submit that reloads with the next set of options. This
     just replaces the reload with a fetch, so a resident on a weak signal is
     not waiting on a full page render between choices.
     ====================================================================== */

  const ReportForm = {
    init() {
      const form = $('[data-report-form]');
      if (!form) return;

      const barangay = $('[data-report-barangay]', form);
      const purok = $('[data-report-purok]', form);
      const property = $('[data-report-property]', form);
      const search = $('[data-report-search]', form);
      const count = $('[data-report-count]', form);
      if (!barangay || !property) return;

      // Which barangay the purok list was last built for. Rebuilding it on a
      // purok change would discard the very choice that triggered the load, so
      // the list is refreshed only when the barangay actually changed. This
      // used to test `document.activeElement === barangay`, which is not the
      // same thing -- a change fired while focus had already moved on left the
      // purok list empty.
      let builtFor = null;

      const load = async () => {
        if (!barangay.value) {
          purok.disabled = true;
          property.disabled = true;
          if (search) search.disabled = true;
          return;
        }

        // A purok belongs to the barangay it was chosen in. Carrying it over
        // to a different barangay filters the property list down to nothing.
        const changed = builtFor !== barangay.value;
        if (changed) purok.value = '';

        const url = new URL(form.dataset.optionsUrl, window.location.origin);
        url.searchParams.set('barangay', barangay.value);
        if (purok.value) url.searchParams.set('purok', purok.value);
        if (search && search.value.trim()) {
          url.searchParams.set('search', search.value.trim());
        }

        let data;
        try {
          data = await fetch(url).then((r) => r.json());
        } catch (err) {
          return;   // the plain-form path still works; leave what is there
        }

        if (changed) {
          builtFor = barangay.value;
          purok.innerHTML = '<option value="">All puroks</option>';
          data.puroks.forEach((p) => {
            const opt = document.createElement('option');
            opt.value = p;
            opt.textContent = p;
            purok.appendChild(opt);
          });
        }

        // A one-line select preselects whatever comes first, so the empty
        // placeholder is rebuilt every time -- without it the form would sit
        // holding a household the resident never chose.
        const term = search ? search.value.trim() : '';
        property.innerHTML = '';
        const blank = document.createElement('option');
        blank.value = '';
        blank.textContent = data.properties.length
          ? 'Select your household or establishment…'
          : (term ? `No match for "${term}"` : 'No properties are registered here yet');
        property.appendChild(blank);

        data.properties.forEach((p) => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = `${p.owner_name} — ${p.purok} (${p.type})`;
          property.appendChild(opt);
        });

        purok.disabled = false;
        property.disabled = false;
        if (search) search.disabled = false;
        if (count) {
          count.textContent = data.properties.length
            ? `${data.properties.length} listed. If yours is missing, ask your `
              + 'barangay office to add it.'
            : (term
                ? 'Nothing matches that name. Clear the search to see the whole list.'
                : 'No households or establishments are registered in this barangay '
                  + 'yet. Ask your barangay office to add yours.');
        }
      };

      barangay.addEventListener('change', load);
      purok.addEventListener('change', load);

      if (search) {
        // Debounced: this hits the server on each change, and a resident
        // typing a name should not send one request per keystroke.
        let timer = null;
        search.addEventListener('input', () => {
          clearTimeout(timer);
          timer = setTimeout(load, 250);
        });
        // Enter in a search box would otherwise submit a form that has no
        // household chosen yet.
        search.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          clearTimeout(timer);
          load();
        });
      }
    },
  };

  /* ======================================================================
     UNTIL FIELD -- "Replacement Until", shown only when it applies

     A permanent assignment has no end date, so the field is hidden and not
     required until Status says Temporary Replacement. The server enforces the
     same rule; this just stops the form asking for a date that means nothing.
     ====================================================================== */

  const UntilField = {
    TEMP: 'Temporary Replacement',

    init() {
      $$('[data-until-field]').forEach((field) => {
        const form = field.closest('form');
        if (!form) return;
        const controls = $$('[name="status"]', form);
        if (!controls.length) return;

        const sync = () => this.apply(field, this.statusOf(controls));
        controls.forEach((c) => c.addEventListener('change', sync));
        // Also after a dialog is filled from a row's data-set-* values.
        form.addEventListener('gcts:filled', sync);
        sync();
      });
    },

    statusOf(controls) {
      const radios = controls.filter((c) => c.type === 'radio');
      if (radios.length) {
        const picked = radios.find((r) => r.checked);
        return picked ? picked.value : '';
      }
      return controls[0].value;
    },

    apply(field, status) {
      const on = status === this.TEMP;
      field.hidden = !on;
      const input = field.querySelector('input');
      if (!input) return;
      input.required = on;
      // A hidden date must not block submission with a stale value.
      if (!on) input.setCustomValidity('');
    },
  };

  /* ======================================================================
     DATE FIELDS -- open the calendar, and apply the choice

     Two complaints, one cause. A bare <input type="date"> only opens its
     calendar from the small glyph at the right-hand edge, and the "View"
     button next to it looked like the way to pick a date when all it did was
     submit -- so tapping the obvious control just reloaded the same day.

     Here the whole field opens the picker, the button beside it does too, and
     choosing a date applies it. showPicker() is guarded: where it is missing
     or refuses, the click falls through and the button still submits, which
     is exactly the old behaviour.
     ====================================================================== */

  const DateField = {
    init() {
      // Every date field, dialogs included. Restricting this to page-level
      // fields left the ones inside a modal behaving differently from the
      // ones outside it, which is the sort of inconsistency that reads as a
      // broken control rather than a deliberate choice.
      this.wire(document);

      // A dialog's fields are in the DOM from the start here, but wiring on
      // open too keeps this correct if any dialog is ever built on demand.
      $$('[data-modal-open]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const modal = document.getElementById(btn.dataset.modalOpen);
          if (modal) this.wire(modal);
        });
      });

      $$('[data-date-picker]').forEach((btn) => {
        const input = document.getElementById(btn.dataset.datePicker);
        if (!input) return;
        btn.addEventListener('click', (e) => {
          if (this.show(input)) e.preventDefault();
        });
      });

      // Applying the choice. Without this the calendar closes and nothing
      // happens until the button is found and pressed a second time.
      $$('[data-date-submit]').forEach((input) => {
        input.addEventListener('change', () => {
          if (input.value && input.form) input.form.submit();
        });
      });
    },

    wire(root) {
      $$('input[type="date"]', root).forEach((input) => {
        if (input.dataset.pickerWired) return;   // once per field
        input.dataset.pickerWired = '1';
        input.addEventListener('click', () => this.show(input));
      });
    },

    show(input) {
      if (typeof input.showPicker !== 'function') return false;
      try {
        input.showPicker();
        return true;
      } catch (err) {
        return false;   // not user-activated, or unsupported -- let it be
      }
    },
  };

  /* ======================================================================
     BOOT
     ====================================================================== */

  function boot() {
    Theme.init();
    Drawer.init();
    Popover.init();
    Modal.init();
    Lightbox.init();
    Toast.init();
    Clock.init();
    Forms.init();
    Segmented.init();
    Reveal.init();
    Stepper.init();
    Geo.init();
    Dropzone.init();
    RoleFields.init();
    Duty.init();
    ReportForm.init();
    DateField.init();
    UntilField.init();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // Expose a tiny surface for page-level scripts.
  window.GCTS = { Modal, Toast, Drawer, Theme };
})();
