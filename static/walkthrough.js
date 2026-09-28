/* ==============================================================================
   WALKTHROUGH — guided tours for the main page
   ==============================================================================
   Two tours, one engine:

     "main"     the input form, top to bottom. Runs itself on a first visit.
     "preview"  the review panel, which only exists after a parse — so it fires
                the first time a preview actually appears, when the controls it
                describes are on screen and mean something.

   The engine drives the page the way a user would (it clicks the real cards and
   accordions) rather than reaching into the page's own script. That keeps this
   file completely decoupled from the inline logic in index.html: nothing here
   knows how parsing, uploads, or the preview are implemented, so neither side
   can break the other. Steps whose target is missing or hidden — the AI picker
   for an account without AI, the corrections row before there is a correction —
   drop out of the tour instead of pointing at nothing.
   ============================================================================== */

(function () {
  'use strict';

  // Bump when the UI changes enough that returning users should see the tour
  // again. Anything else would need a per-user flag on the server, which is far
  // more machinery than a product tour is worth.
  var VERSION = 'v1';
  var KEY = 'quizify-tour-';
  var GAP = 14;            // popover-to-target distance
  var EDGE = 12;           // minimum distance from the viewport edge
  var PAD = 8;             // spotlight padding around the target
  var MOBILE = 640;        // below this the card docks instead of floating

  var reduced = window.matchMedia &&
                window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // --------------------------------------------------------------------------
  // Storage — Safari private mode throws on write, so every access is guarded.
  // A failure reads as "already seen": a tour that cannot remember being
  // dismissed would restart on every page load, which is worse than not running.
  // --------------------------------------------------------------------------

  function seen(id) {
    try { return localStorage.getItem(KEY + id) === VERSION; } catch (e) { return true; }
  }

  function markSeen(id) {
    try { localStorage.setItem(KEY + id, VERSION); } catch (e) { /* nothing to do */ }
  }

  // --------------------------------------------------------------------------
  // DOM helpers
  // --------------------------------------------------------------------------

  function $(sel) { return document.querySelector(sel); }

  function visible(el) {
    if (!el) return false;
    if (el.hidden || el.closest('[hidden]')) return false;
    var rect = el.getBoundingClientRect();
    if (!rect.width && !rect.height) return false;
    var style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  }

  function icon(paths) {
    return '<svg viewBox="0 0 24 24">' + paths + '</svg>';
  }

  // --------------------------------------------------------------------------
  // Overlay elements — built once, on first run, and reused after that
  // --------------------------------------------------------------------------

  var el = null;   // { scrim, guards: [...], pop, title, body, step, dots, back, next }

  function build() {
    if (el) return el;

    var guards = [];
    for (var i = 0; i < 5; i++) {
      var guard = document.createElement('div');
      guard.className = 'wt-guard' + (i === 4 ? ' wt-guard--hole' : '');
      guard.addEventListener('click', nudge);
      guard.style.width = '0px';       // parked until a step positions them, so an
      guard.style.height = '0px';      // idle overlay never swallows a real click
      document.body.appendChild(guard);
      guards.push(guard);
    }

    var scrim = document.createElement('div');
    scrim.className = 'wt-scrim';
    document.body.appendChild(scrim);

    var pop = document.createElement('div');
    pop.className = 'wt-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'true');
    pop.setAttribute('aria-labelledby', 'wtTitle');
    pop.setAttribute('tabindex', '-1');
    pop.innerHTML =
      '<div class="wt-pop-arrow"></div>' +
      '<div class="wt-pop-head">' +
        '<span class="wt-pop-step"></span>' +
        '<button type="button" class="wt-pop-close" data-act="end" aria-label="End tour">' +
          icon('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>') +
        '</button>' +
      '</div>' +
      '<h4 class="wt-pop-title" id="wtTitle"></h4>' +
      '<div class="wt-pop-body"></div>' +
      '<div class="wt-pop-foot">' +
        '<div class="wt-dots"></div>' +
        '<div class="wt-pop-actions">' +
          '<button type="button" class="wt-skip" data-act="end">Skip</button>' +
          '<button type="button" class="wt-btn wt-btn--ghost" data-act="back">Back</button>' +
          '<button type="button" class="wt-btn wt-btn--primary" data-act="next">Next</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(pop);

    pop.addEventListener('click', function (event) {
      var act = event.target.closest('[data-act]');
      if (act) {
        if (act.dataset.act === 'next') next();
        else if (act.dataset.act === 'back') back();
        else end();
        return;
      }
      var dot = event.target.closest('.wt-dot');
      if (dot) go(parseInt(dot.dataset.i, 10));
    });

    el = {
      scrim: scrim,
      guards: guards,
      pop: pop,
      title: pop.querySelector('.wt-pop-title'),
      body: pop.querySelector('.wt-pop-body'),
      step: pop.querySelector('.wt-pop-step'),
      dots: pop.querySelector('.wt-dots'),
      back: pop.querySelector('[data-act="back"]'),
      next: pop.querySelector('[data-act="next"]')
    };
    return el;
  }

  function nudge() {
    if (!el) return;
    el.pop.classList.remove('wt-pop--nudge');
    void el.pop.offsetWidth;           // restart the animation
    el.pop.classList.add('wt-pop--nudge');
    el.pop.focus();
  }

  // --------------------------------------------------------------------------
  // Runner state
  // --------------------------------------------------------------------------

  var tour = null;      // { id, steps }
  var steps = [];       // this run's steps, after dropping the ones with no target
  var index = -1;
  var target = null;    // current step's element, or null for a centred step
  var restore = null;   // page state captured at start, put back at the end

  function running() { return tour !== null; }

  // --------------------------------------------------------------------------
  // Positioning
  // --------------------------------------------------------------------------

  function hole() {
    if (!target) return null;
    var rect = target.getBoundingClientRect();
    var pad = (steps[index] && steps[index].pad !== undefined) ? steps[index].pad : PAD;
    return {
      top: Math.max(rect.top - pad, 0),
      left: Math.max(rect.left - pad, 0),
      right: Math.min(rect.right + pad, window.innerWidth),
      bottom: Math.min(rect.bottom + pad, window.innerHeight),
      width: 0, height: 0   // filled below
    };
  }

  function layout() {
    if (!running()) return;

    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var box = hole();
    if (box) {
      box.width = Math.max(box.right - box.left, 0);
      box.height = Math.max(box.bottom - box.top, 0);
    }

    // The card is placed before the spotlight is painted, because on a phone it
    // claims a whole edge of the screen. A target too tall to sit beside it gets
    // its spotlight trimmed to the space that is left — framing the part the user
    // can actually see beats drawing a highlight underneath the instructions.
    var taken = positionPop(box, vw, vh);
    if (taken && box && box.width && box.height) trim(box, taken);

    // --- spotlight -----------------------------------------------------------
    var scrim = el.scrim;
    if (box && box.width && box.height) {
      scrim.classList.remove('wt-scrim--flat');
      scrim.style.top = box.top + 'px';
      scrim.style.left = box.left + 'px';
      scrim.style.width = box.width + 'px';
      scrim.style.height = box.height + 'px';
    } else {
      // No target: the hole collapses off-screen so the page dims evenly.
      scrim.classList.add('wt-scrim--flat');
      scrim.style.top = '-10px';
      scrim.style.left = '-10px';
      scrim.style.width = '0px';
      scrim.style.height = '0px';
    }

    // --- click guards --------------------------------------------------------
    // Four panels around the hole, plus one over it that is removed only when
    // the step wants the user to interact with what is highlighted.
    var g = el.guards;
    var b = (box && box.width && box.height) ? box : { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };

    place(g[0], 0, 0, vw, b.top);                              // above
    place(g[1], b.bottom, 0, vw, Math.max(vh - b.bottom, 0));  // below
    place(g[2], b.top, 0, b.left, b.height);                   // left
    place(g[3], b.top, b.right, Math.max(vw - b.right, 0), b.height);
    place(g[4], b.top, b.left, b.width, b.height);             // the hole itself
    g[4].hidden = !!(steps[index] && steps[index].interactive);
  }

  // Pull the hole clear of the docked card. Skipped when what is left would be a
  // sliver, since a 20px band of highlight tells the user nothing — better to let
  // the card overlap and keep the shape of the thing recognisable.
  function trim(box, taken) {
    var top = taken.dock === 'top' ? Math.max(box.top, taken.bottom + GAP) : box.top;
    var bottom = taken.dock === 'top' ? box.bottom : Math.min(box.bottom, taken.top - GAP);
    if (bottom - top < 48) return;
    box.top = top;
    box.bottom = bottom;
    box.height = bottom - top;
  }

  // Scroll fires far more often than the overlay needs to move, and each layout
  // reads geometry back from the DOM. One recalculation per frame is plenty.
  var pending = false;

  function reflow() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () {
      pending = false;
      layout();
    });
  }

  function place(node, top, left, width, height) {
    node.style.top = top + 'px';
    node.style.left = left + 'px';
    node.style.width = Math.max(width, 0) + 'px';
    node.style.height = Math.max(height, 0) + 'px';
  }

  function positionPop(box, vw, vh) {
    var pop = el.pop;
    pop.classList.remove('wt-pop--top', 'wt-pop--bottom', 'wt-pop--left',
                         'wt-pop--right', 'wt-pop--center', 'wt-pop--sheet',
                         'wt-pop--sheet-top');

    var pw = pop.offsetWidth;
    var ph = pop.offsetHeight;

    // Centred, modal-style: the step has nothing to point at.
    if (!box || !box.width || !box.height) {
      pop.classList.add('wt-pop--center');
      pop.style.top = Math.max((vh - pop.offsetHeight) / 2, EDGE) + 'px';
      pop.style.left = Math.max((vw - pop.offsetWidth) / 2, EDGE) + 'px';
      return;
    }

    if (vw <= MOBILE) {
      pop.classList.add('wt-pop--sheet');
      pop.style.top = '';
      pop.style.left = '';

      // A phone-height card takes up most of the screen, so a target low on the
      // page ends up underneath the very instructions describing it. Dock at the
      // top instead — but only when there is genuinely room above the target,
      // otherwise flipping just moves the overlap to the other end.
      var sheetH = pop.offsetHeight;                 // measured with the class on
      var covered = box.bottom > vh - EDGE - sheetH;
      var atTop = covered && box.top > sheetH + EDGE * 2;
      pop.classList.toggle('wt-pop--sheet-top', atTop);

      return {
        dock: atTop ? 'top' : 'bottom',
        top: atTop ? EDGE : vh - EDGE - sheetH,
        bottom: atTop ? EDGE + sheetH : vh - EDGE
      };
    }

    var wanted = steps[index].place;
    var order = (wanted ? [wanted] : []).concat(['bottom', 'top', 'right', 'left']);
    var pick = null;

    for (var i = 0; i < order.length && !pick; i++) {
      var side = order[i];
      if (side === 'bottom' && box.bottom + GAP + ph <= vh - EDGE) pick = side;
      if (side === 'top' && box.top - GAP - ph >= EDGE) pick = side;
      if (side === 'right' && box.right + GAP + pw <= vw - EDGE) pick = side;
      if (side === 'left' && box.left - GAP - pw >= EDGE) pick = side;
    }

    // Nothing fits (a target taller than the viewport, usually) — take the side
    // with the most room and let the clamp below keep the card on screen.
    if (!pick) {
      var room = [
        { side: 'bottom', space: vh - box.bottom },
        { side: 'top', space: box.top },
        { side: 'right', space: vw - box.right },
        { side: 'left', space: box.left }
      ].sort(function (a, c) { return c.space - a.space; });
      pick = room[0].side;
    }

    var top, left;
    if (pick === 'bottom' || pick === 'top') {
      top = pick === 'bottom' ? box.bottom + GAP : box.top - GAP - ph;
      left = box.left + box.width / 2 - pw / 2;
    } else {
      left = pick === 'right' ? box.right + GAP : box.left - GAP - pw;
      top = box.top + box.height / 2 - ph / 2;
    }

    top = Math.min(Math.max(top, EDGE), Math.max(vh - ph - EDGE, EDGE));
    left = Math.min(Math.max(left, EDGE), Math.max(vw - pw - EDGE, EDGE));

    pop.classList.add('wt-pop--' + pick);
    pop.style.top = top + 'px';
    pop.style.left = left + 'px';

    // Arrow tracks the target's centre, not the card's, so it still points at the
    // right thing after the clamp above has shifted the card sideways.
    var arrow = pop.querySelector('.wt-pop-arrow');
    if (pick === 'bottom' || pick === 'top') {
      arrow.style.left = clamp(box.left + box.width / 2 - left, 16, pw - 16) - 6 + 'px';
      arrow.style.top = '';
    } else {
      arrow.style.top = clamp(box.top + box.height / 2 - top, 16, ph - 16) - 6 + 'px';
      arrow.style.left = '';
    }
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), Math.max(min, max));
  }

  // --------------------------------------------------------------------------
  // Scrolling a target into view
  // --------------------------------------------------------------------------

  // The form and the main panel each scroll independently, so rather than guess
  // which one owns the target, hand it to the browser and wait for the rect to
  // settle. Positioning against a still-moving element is what makes tours look
  // broken, and the cap means a target that never settles cannot hang the tour.
  var marked = null;   // the target currently carrying the scroll-margin class

  function unmark() {
    if (marked) marked.classList.remove('wt-target');
    marked = null;
  }

  function reveal(node, done) {
    unmark();
    if (!node) return done();

    // The class only adds scroll-margin, so the target never lands flush against
    // an edge with its ring clipped. It is removed as soon as the step changes.
    node.classList.add('wt-target');
    marked = node;

    node.scrollIntoView({
      // A phone keeps the target high and the docked card below it; a desktop has
      // room on every side, so centring reads better there.
      block: window.innerWidth <= MOBILE ? 'start' : 'center',
      inline: 'nearest',
      behavior: reduced ? 'auto' : 'smooth'
    });

    settle(node, function () {
      if (!lift(node)) return done();
      settle(node, done);
    });
  }

  function settle(node, done) {
    var last = null;
    var stable = 0;
    var started = Date.now();

    (function tick() {
      var rect = node.getBoundingClientRect();
      if (last && Math.abs(rect.top - last.top) < 0.5 && Math.abs(rect.left - last.left) < 0.5) {
        stable++;
      } else {
        stable = 0;
      }
      last = rect;
      if (stable >= 2 || Date.now() - started > 700) return done();
      requestAnimationFrame(tick);
    })();
  }

  // scrollIntoView aligns within the nearest scrolling ancestor, which on a phone
  // can still leave the target a hundred-odd pixels down the screen — enough that
  // a tall target and the docked card no longer both fit. This takes up the slack
  // with a window scroll, and only ever moves the target UP, never out of view.
  // Returns whether the page actually moved, so the caller knows to wait again.
  function lift(node) {
    if (window.innerWidth > MOBILE) return false;
    var slack = node.getBoundingClientRect().top - (EDGE + PAD);
    if (slack <= 1) return false;
    window.scrollBy({ top: slack, behavior: reduced ? 'auto' : 'smooth' });
    return true;
  }

  // --------------------------------------------------------------------------
  // Rendering a step
  // --------------------------------------------------------------------------

  function render() {
    var step = steps[index];

    el.title.textContent = step.title;
    el.body.innerHTML = step.body +
      (step.tryIt
        ? '<div class="wt-pop-try">' +
            icon('<path d="M9 11.5V6a2 2 0 0 1 4 0v5"/>' +
                 '<path d="M13 9.5a2 2 0 0 1 4 0V15a5 5 0 0 1-5 5h-1a6 6 0 0 1-5-3l-2-3.5a2 2 0 0 1 3-2.5"/>') +
            '<span>' + step.tryIt + '</span>' +
          '</div>'
        : '');
    el.step.textContent = 'Step ' + (index + 1) + ' of ' + steps.length;

    el.dots.innerHTML = steps.map(function (s, i) {
      var cls = i === index ? ' wt-dot--now' : (i < index ? ' wt-dot--done' : '');
      return '<button type="button" class="wt-dot' + cls + '" data-i="' + i +
             '" aria-label="Step ' + (i + 1) + '"></button>';
    }).join('');

    el.back.style.display = index === 0 ? 'none' : '';
    el.next.textContent = index === steps.length - 1 ? 'Finish' : 'Next';

    // Pulse only where the user is being asked to act, so the animation carries
    // meaning instead of being decoration on every step.
    el.scrim.classList.toggle('wt-scrim--pulse', !!step.tryIt);
  }

  function go(to) {
    if (!running() || to < 0 || to >= steps.length) return;
    index = to;

    var step = steps[index];
    if (step.before) step.before();

    target = step.target ? $(step.target) : null;
    if (target && !visible(target)) target = null;

    render();
    reveal(target, function () {
      layout();
      el.pop.classList.add('wt-pop--shown');
      el.pop.focus({ preventScroll: true });
    });
  }

  function next() {
    if (index >= steps.length - 1) return finish();
    go(index + 1);
  }

  function back() { go(index - 1); }

  // --------------------------------------------------------------------------
  // Start / end
  // --------------------------------------------------------------------------

  function start(which) {
    var def = TOURS[which];
    if (!def || running()) return;

    build();

    // Taken before the filter below, because deciding whether a step has a target
    // means running its `before` hook — which opens accordions and flips cards.
    // Snapshotting after that would record the tour's own mess as the user's state.
    restore = snapshot();

    // Only steps that actually have something to point at. A step with no target
    // at all is intentional (the welcome and sign-off cards) and always stays.
    steps = def.steps.filter(function (step) {
      if (!step.target) return true;
      if (step.before) step.before();
      return visible($(step.target));
    });
    if (!steps.length) {
      restore();
      restore = null;
      return;
    }

    tour = def;
    index = -1;

    // Switch the overlay back on only once there is definitely a tour to show.
    el.scrim.classList.remove('wt-scrim--off');
    el.guards.forEach(function (guard) { guard.classList.remove('wt-guard--off'); });

    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', reflow);
    window.addEventListener('scroll', reflow, true);

    go(0);
  }

  function end(finished) {
    if (!running()) return;

    markSeen(tour.id);
    unmark();
    tour = null;
    steps = [];
    index = -1;
    target = null;

    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', reflow);
    window.removeEventListener('scroll', reflow, true);

    el.pop.classList.remove('wt-pop--shown');
    el.scrim.classList.remove('wt-scrim--pulse');

    // Switching the overlay off, rather than removing the nodes, so the next run
    // reuses them and the fade-out is allowed to finish. Both classes matter: the
    // scrim dims via a huge box-shadow, which keeps painting no matter how small
    // the element is, so it has to be told to stop drawing.
    el.scrim.classList.add('wt-scrim--off');
    el.guards.forEach(function (guard) {
      guard.classList.add('wt-guard--off');
      place(guard, 0, 0, 0, 0);
    });
    el.scrim.style.width = '0px';
    el.scrim.style.height = '0px';

    if (restore) restore();
    restore = null;

    var btn = $('#walkthroughBtn');
    if (btn) {
      btn.classList.remove('tour-btn--hint');
      if (finished) btn.focus({ preventScroll: true });
    }
  }

  function finish() { end(true); }

  function onKey(event) {
    if (!running()) return;

    if (event.key === 'Escape') {
      event.preventDefault();
      return end();
    }

    // An interactive step invites the user into the textarea or a field; stealing
    // their arrow keys to advance the tour would make the caret unmovable. Escape
    // stays live, because leaving must always work.
    var node = event.target;
    if (node && (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA' ||
                 node.tagName === 'SELECT' || node.isContentEditable)) {
      return;
    }

    if (event.key === 'ArrowRight') {
      event.preventDefault();
      return next();
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      return back();
    }
    if (event.key === 'Enter' && event.target === el.pop) {
      event.preventDefault();
      return next();
    }
    // Keep Tab inside the card, except on a step that asks the user to go and
    // use the highlighted control — there, tabbing out is the whole point.
    if (event.key === 'Tab' && !steps[index].interactive) {
      var focusable = el.pop.querySelectorAll('button:not([style*="display: none"])');
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      var active = document.activeElement;
      if (!el.pop.contains(active)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  // A tour that flips cards and opens accordions would otherwise hand the page
  // back in a state the user never chose. This records what was open, and the
  // returned function puts it back.
  function snapshot() {
    var method = document.querySelector('.method-card.active');
    var docs = document.querySelector('.docs-content.open');
    var methodId = method ? method.id : null;
    var docsId = docs ? docs.id : null;

    return function () {
      if (methodId) {
        var card = document.getElementById(methodId);
        if (card && !card.classList.contains('active')) card.click();
      }
      var openNow = document.querySelector('.docs-content.open');
      var openId = openNow ? openNow.id : null;
      if (openId !== docsId) {
        if (openNow) toggleDocs(openId);          // close whatever the tour opened
        if (docsId) toggleDocs(docsId);           // and reopen what the user had
      }
    };
  }

  function toggleDocs(id) {
    var btn = document.querySelector('.docs-toggle[data-target="' + id + '"]');
    if (btn) btn.click();
  }

  // --------------------------------------------------------------------------
  // Step helpers
  // --------------------------------------------------------------------------

  function useMethod(which) {
    return function () {
      var card = document.getElementById(which === 'upload' ? 'uploadCard' : 'pasteCard');
      if (card && !card.classList.contains('active')) card.click();
    };
  }

  function openDocs(id) {
    return function () {
      var content = document.getElementById(id);
      if (content && !content.classList.contains('open')) toggleDocs(id);
    };
  }

  // --------------------------------------------------------------------------
  // The tours
  // --------------------------------------------------------------------------

  var TOURS = {
    main: {
      id: 'main',
      steps: [
        {
          title: 'Welcome to Quizify',
          body: 'This turns a quiz — a Word file, a PDF, or text you paste — into a ' +
                'clean spreadsheet with the questions, options and answers in columns. ' +
                '<b>About a minute</b> to walk through it, and you can leave any time with ' +
                '<code>Esc</code>.'
        },
        {
          target: '.method-cards',
          place: 'bottom',
          title: 'Start with how the quiz reaches us',
          body: 'Two ways in: <b>upload a file</b>, or <b>paste the text</b>. Pick either — ' +
                'everything after this point is identical.'
        },
        {
          target: '#dropZone',
          place: 'bottom',
          before: useMethod('upload'),
          title: 'Drop a file, or click to browse',
          body: 'Takes <code>.docx</code>, <code>.pdf</code>, <code>.txt</code>, ' +
                '<code>.csv</code>, <code>.xlsx</code>, <code>.json</code> and more. In a ' +
                'DOCX, a <b>bolded option is read as the correct answer</b>, so a document ' +
                'with no answer key often still comes out fully answered.',
          tryIt: 'Drag a file onto the dashed area to try it now.',
          interactive: true
        },
        {
          target: '#quizText',
          place: 'left',
          before: useMethod('paste'),
          title: 'Or paste the questions straight in',
          body: 'Numbering can be <code>Q1.</code>, <code>1.</code>, <code>(1)</code> or ' +
                '<code>Q.No.1</code>; options <code>A.</code>, <code>(a)</code> or ' +
                '<code>(i)</code>. True/false, fill-in-the-blank, matching and ' +
                'assertion-reason questions are recognised too.',
          interactive: true
        },
        {
          target: '.form-grid',
          place: 'top',
          title: 'Label the output',
          body: '<b>Subject</b> and <b>Topic</b> become columns in the export — they are how ' +
                'the file stays useful once it is one of many. <b>Max answers</b> caps how ' +
                'many option columns get written.'
        },
        {
          target: '#ai_mode',
          place: 'top',
          title: 'AI assist, when the rules fall short',
          body: 'Most documents are handled by pattern rules alone, in well under a second. ' +
                '<b>Auto</b> brings in AI only for the parts the rules could not read. ' +
                '<b>Off</b> is rules-only and near-instant; <b>Always</b> is the slowest and ' +
                'is worth it for messy scans and unusual layouts.'
        },
        {
          target: '#submitBtn',
          place: 'top',
          title: 'Process, then look before you download',
          body: 'This does not hand you a file straight away. It parses first and shows you ' +
                'every question it found, so a bad extraction is caught here rather than in a ' +
                'spreadsheet you have already sent on.'
        },
        {
          target: '#howItWorksContent',
          place: 'right',
          before: openDocs('howItWorksContent'),
          title: 'The reference panel',
          body: 'Every supported question, option and answer pattern is listed here, along ' +
                'with troubleshooting for the two things that usually go wrong: ' +
                '<b>no questions found</b> and <b>wrong answers extracted</b>.'
        },
        {
          target: '#chatToggle',
          place: 'left',
          title: 'Ask, rather than hunt',
          body: 'Quizify AI answers questions about formats, uploads and errors without ' +
                'you leaving the page.'
        },
        {
          target: '#walkthroughBtn',
          place: 'right',
          title: 'That is the whole flow',
          body: 'Upload or paste, label it, process, check, download. This tour is here ' +
                'whenever you want it again — and once a preview is open, the same button ' +
                'walks you through <b>reviewing</b> the results.'
        }
      ]
    },

    preview: {
      id: 'preview',
      steps: [
        {
          target: '#previewStats',
          place: 'bottom',
          title: 'What came out',
          body: 'How many questions were found, how many have an answer, and the average ' +
                'confidence. <b>Missing answer</b> is the number worth reading first — those ' +
                'rows export blank unless you fill them in below.'
        },
        {
          target: '.preview-q',
          place: 'right',
          title: 'Every question, before it becomes a file',
          body: 'The correct option is highlighted on each card. Tags flag anything that ' +
                'needs you: <b>Check</b> for a low-confidence parse, <b>No answer</b> where ' +
                'none was found, <b>Used before</b> for a question you have exported previously.'
        },
        {
          target: '.preview-edit',
          place: 'left',
          title: 'Fix it here, not in Excel',
          body: 'Edit opens the question inline — reword it, change its type, add or remove ' +
                'options, and set the correct one. Corrections apply to the download ' +
                'immediately.',
          tryIt: 'Click Edit on any question to see the editor.',
          interactive: true
        },
        {
          target: '.preview-toolbar',
          place: 'bottom',
          title: 'Cut a long list down',
          body: 'Filter by text, or tick <b>Needs review only</b> to see just the questions ' +
                'that were flagged. On a hundred-question document this is the difference ' +
                'between checking everything and checking what matters.'
        },
        {
          target: '#previewLearnRow',
          place: 'top',
          title: 'Teach it your corrections',
          body: 'When you have corrected something, this offers to remember it — so the same ' +
                'pattern is parsed your way next time instead of being corrected again.'
        },
        {
          target: '.preview-actions',
          place: 'top',
          title: 'Download in any format, free',
          body: 'The export reuses what is already on screen, so switching between Excel, CSV ' +
                'and JSON <b>never re-parses the document</b> — and never spends another AI ' +
                'call. Download all three if you want them.'
        },
        {
          target: '#previewBack',
          place: 'bottom',
          title: 'Back for the next one',
          body: 'Returns to the form with your settings intact, ready for the next file.'
        }
      ]
    }
  };

  // --------------------------------------------------------------------------
  // Wiring
  // --------------------------------------------------------------------------

  function init() {
    var btn = document.getElementById('walkthroughBtn');
    var panel = document.getElementById('previewPanel');

    if (btn) {
      // The button starts whichever tour matches what is on screen: explaining
      // the input form while the user is staring at results would be useless.
      btn.addEventListener('click', function () {
        start(panel && visible(panel) ? 'preview' : 'main');
      });
      if (!seen('main')) btn.classList.add('tour-btn--hint');
    }

    // The preview tour cannot run on load — the panel it describes does not exist
    // yet. It waits for the first real preview instead.
    if (panel) {
      new MutationObserver(function () {
        if (panel.hidden || running() || seen('preview')) return;
        setTimeout(function () {
          if (!panel.hidden && !running() && !seen('preview')) start('preview');
        }, 1100);   // let the results land and be looked at first
      }).observe(panel, { attributes: true, attributeFilter: ['hidden'] });
    }

    // First visit runs the tour by itself; ?tour=1 forces it for anyone.
    var forced = /[?&]tour=1(&|$)/.test(window.location.search);
    if (forced || !seen('main')) {
      setTimeout(function () { start('main'); }, forced ? 200 : 800);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Small surface for the console and for any page that wants to launch a tour.
  window.QuizifyWalkthrough = {
    start: start,
    end: end,
    reset: function () {
      try {
        localStorage.removeItem(KEY + 'main');
        localStorage.removeItem(KEY + 'preview');
      } catch (e) { /* nothing to do */ }
    }
  };
})();
