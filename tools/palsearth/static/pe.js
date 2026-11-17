/* PALs Earth — progressive enhancement. Every flow below also works with JS
   off, because each one is a real form POST; this only removes friction. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ── toasts fade themselves out ─────────────────────────── */
  $$(".pe-toast").forEach(function (t) {
    setTimeout(function () {
      t.style.transition = "opacity .4s,transform .4s";
      t.style.opacity = "0";
      t.style.transform = "translateY(6px)";
      setTimeout(function () { t.remove(); }, 450);
    }, 3600);
  });

  /* ── mobile sidebar drawer ──────────────────────────────── */
  var side = $("#pe-side"), burger = $("#pe-burger"), scrim = $("#pe-scrim");
  function drawer(open) {
    if (!side) return;
    side.classList.toggle("open", open);
    if (scrim) scrim.classList.toggle("show", open);
    if (burger) burger.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (burger) burger.addEventListener("click", function () { drawer(!side.classList.contains("open")); });
  if (scrim) scrim.addEventListener("click", function () { drawer(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") drawer(false); });

  /* ── password reveal + strength ─────────────────────────── */
  $$("[data-pw-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = $("[data-pw]", btn.closest("label") || document);
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
    });
  });

  var pw = $("[data-pw]"), meter = $("[data-strength]");
  if (pw && meter) {
    var bars = $$("i", meter), label = $("[data-strength-label]");
    var COLORS = ["#e2ddd0", "#e8663d", "#e0a21a", "#7fc15a", "#2f9e5b"];
    var WORDS = ["Too short", "Weak", "Okay", "Strong", "Excellent"];
    pw.addEventListener("input", function () {
      var v = pw.value;
      meter.hidden = false;
      var score = [v.length >= 8, /\d/.test(v), /[A-Z]/.test(v), /[^A-Za-z0-9]/.test(v)]
        .filter(Boolean).length;
      bars.forEach(function (b, i) { b.style.background = i < score ? COLORS[score] : "#e2ddd0"; });
      if (label) {
        label.textContent = v ? WORDS[score]
          : "Use 8+ characters with a number and a symbol";
      }
    });
  }

  /* ── upload: drag/drop + submit as soon as a file is chosen ─ */
  var fileInput = $("#pe-file"), drop = $("#pe-drop"), upForm = $("#pe-upload-form");
  if (fileInput && upForm) {
    fileInput.addEventListener("change", function () {
      if (fileInput.files && fileInput.files.length) {
        if (drop) {
          drop.classList.add("over");
          $("span:nth-child(2)", drop).textContent = "Reading " + fileInput.files[0].name + "…";
        }
        upForm.submit();
      }
    });
  }
  if (drop && fileInput) {
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        fileInput.dispatchEvent(new Event("change"));
      }
    });
  }

  /* ── manual builder: counts, summary, conditional panels ── */
  var manual = $("#pe-manual");
  if (manual) {
    var dsBoxes = $$('input[name="datasets"]', manual);
    var statBoxes = $$('input[name="stats"]', manual);
    var count = $("#pe-selcount"), summary = $("#pe-summary");
    var ghsl = $("[data-ghsl]", manual);
    var tempUnit = $("[data-temp-unit]", manual);
    var modeField = $("#pe-date-mode");
    var modeSelect = $("#pe-mode-select");
    var modeSelectWrap = modeSelect ? modeSelect.closest("label") : null;
    var panels = {
      range: $('[data-mode-panel="range"]', manual),
      pregnancy_windows: $('[data-mode-panel="pregnancy_windows"]', manual),
      index_window: $('[data-mode-panel="index_window"]', manual),
      case_crossover: $('[data-mode-panel="case_crossover"]', manual)
    };
    var staticNote = $("[data-static-note]", manual);

    function refresh() {
      var chosen = dsBoxes.filter(function (b) { return b.checked; });
      var stats = statBoxes.filter(function (b) { return b.checked; }).length;
      var allStatic = chosen.length > 0 && chosen.every(function (b) { return b.dataset.ts === "0"; });
      var mode = (modeSelect && !allStatic) ? modeSelect.value : "range";
      if (count) count.textContent = chosen.length;
      if (ghsl) ghsl.hidden = !chosen.some(function (b) { return b.dataset.years === "1"; });
      if (tempUnit) tempUnit.hidden = !chosen.some(function (b) { return b.dataset.kelvin === "1"; });
      if (staticNote) staticNote.hidden = !allStatic;
      if (modeSelectWrap) modeSelectWrap.hidden = allStatic;
      Object.keys(panels).forEach(function (key) {
        if (panels[key]) panels[key].hidden = allStatic || mode !== key;
      });
      if (modeField) modeField.value = mode;
      if (summary) {
        summary.textContent = chosen.length
          ? chosen.length + (chosen.length === 1 ? " dataset" : " datasets") +
            " · " + (stats || 1) + (stats === 1 ? " summary" : " summaries")
          : "Pick at least one dataset.";
      }
    }
    dsBoxes.concat(statBoxes).forEach(function (b) { b.addEventListener("change", refresh); });
    if (modeSelect) modeSelect.addEventListener("change", refresh);
    refresh();

    manual.addEventListener("submit", function (e) {
      if (!dsBoxes.some(function (b) { return b.checked; })) {
        e.preventDefault();
        flash("Pick at least one dataset first.");
      }
    });
  }

  /* ── chat: suggestions, autosize, Enter to send ─────────── */
  var askForm = $("#pe-ask-form");
  if (askForm) {
    var ta = $("textarea", askForm), sendBtn = $("button", askForm);
    function autosize() {
      if (!ta) return;
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
    }
    if (ta) {
      ta.addEventListener("input", autosize);
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askForm.requestSubmit(); }
      });
      autosize();
      ta.focus();
    }
    $$("[data-ask]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!ta) return;
        ta.value = btn.dataset.ask;
        autosize();
        askForm.requestSubmit();
      });
    });
    askForm.addEventListener("submit", function () {
      if (ta && !ta.value.trim()) return;
      if (sendBtn) {
        sendBtn.disabled = true;
        sendBtn.innerHTML = '<span class="pe-spin"></span>';
      }
      var wait = document.createElement("span");
      wait.className = "pe-small pe-muted";
      wait.textContent = "Shmron AI is thinking…";
      askForm.parentNode.insertBefore(wait, askForm);
    });
    var chat = $("#pe-chat");
    if (chat) {
      var msgs = $$(".pe-msg", chat);
      if (msgs.length) msgs[msgs.length - 1].scrollIntoView({ block: "nearest" });
    }
  }

  /* ── confirm destructive forms ──────────────────────────── */
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.dataset.confirm)) e.preventDefault();
    });
  });

  /* ── live job polling ───────────────────────────────────── */
  var list = $("#pe-joblist");
  if (list && list.dataset.active === "1") {
    var url = list.dataset.poll, misses = 0;
    var tick = setInterval(function () {
      fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (data) {
          misses = 0;
          var stillActive = false;
          (data.stats || []).forEach(function (s) {
            var el = $('[data-stat="' + s.key + '"]');
            if (el) el.textContent = s.value;
          });
          (data.jobs || []).forEach(function (j) {
            var card = $('[data-job="' + j.id + '"]');
            if (!card) return;
            if (j.is_running) stillActive = true;
            var prog = $("[data-progress]", card);
            if (prog) {
              prog.hidden = !j.is_running;
              var bar = $("i", prog), pct = $("[data-pct]", prog);
              if (bar) bar.style.width = j.progress + "%";
              if (pct) pct.textContent = j.progress + "%";
            }
            var lab = $("[data-status-label]", card);
            if (lab && lab.textContent !== j.status_label) {
              lab.textContent = j.status_label;
              var pill = $("[data-pill]", card);
              if (pill) {
                pill.style.background = j.pill_bg;
                pill.style.color = j.pill_fg;
                var dot = $(".pe-dot", pill);
                if (dot) {
                  dot.style.background = j.pill_fg;
                  dot.style.animation = j.is_running ? "pe-pulse 1.4s infinite" : "none";
                }
              }
              /* a job that just finished or failed grows new buttons — the
                 server renders those, so reload once rather than rebuild here */
              setTimeout(function () { location.reload(); }, 600);
            }
          });
          if (!stillActive) clearInterval(tick);
        })
        .catch(function () { if (++misses >= 5) clearInterval(tick); });
    }, 2500);
  }

  /* ── client-side toast helper ───────────────────────────── */
  function flash(msg) {
    var box = $("#pe-toasts");
    if (!box) return;
    var t = document.createElement("div");
    t.className = "pe-toast err";
    t.innerHTML = '<span class="pe-dot"></span>';
    t.appendChild(document.createTextNode(msg));
    box.appendChild(t);
    setTimeout(function () { t.remove(); }, 3600);
  }
})();
