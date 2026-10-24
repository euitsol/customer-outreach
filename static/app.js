/* Outreach panel - progressive enhancement only; every page works with plain forms. */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const csrf = () => ($("meta[name=csrf]") || {}).content || "";

  /* ---- theme ---- */
  const saved = (() => { try { return localStorage.getItem("theme"); } catch (e) { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  const themeBtn = $("#theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", () => {
    const dark = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim().startsWith("#0");
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
  });

  /* ---- mobile nav ---- */
  const burger = $("#nav-toggle");
  if (burger) burger.addEventListener("click", () => document.body.classList.toggle("nav-open"));
  document.addEventListener("click", e => {
    if (document.body.classList.contains("nav-open") && !e.target.closest(".side") && e.target !== burger)
      document.body.classList.remove("nav-open");
  });

  /* ---- confirm dialogs: <form data-confirm="..."> or <button data-confirm="..."> ---- */
  document.addEventListener("submit", e => {
    const msg = (e.submitter && e.submitter.dataset.confirm) || e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  /* ---- flash dismiss ---- */
  document.addEventListener("click", e => {
    const b = e.target.closest(".flash button");
    if (b) b.parentElement.remove();
  });
  $$(".flash.ok").forEach(f => setTimeout(() => f.remove(), 7000));

  /* ---- filters: auto-submit on change ---- */
  $$("form[data-autosubmit]").forEach(f => {
    $$("select, input[type=date], input[type=checkbox]", f).forEach(el => el.addEventListener("change", () => f.submit()));
  });

  /* ---- bulk selection on tables ---- */
  const bulk = $("#bulk-form");
  if (bulk) {
    const checks = () => $$(".row-check", bulk.closest("[data-bulk-scope]") || document);
    const bar = $("#bulkbar");
    const countEl = $("#sel-count");
    const allNote = $("#selectall-note");
    const selectAllField = $("#select_all_field");
    const update = () => {
      const n = checks().filter(c => c.checked).length;
      if (countEl) countEl.textContent = n;
      if (bar) bar.classList.toggle("hidden", n === 0 && !(selectAllField && selectAllField.value === "1"));
      if (allNote) allNote.classList.toggle("hidden", !(n > 0 && n === checks().length && allNote.dataset.more === "1"));
      if (n < checks().length && selectAllField && selectAllField.value === "1") {
        selectAllField.value = "0";
        if (countEl) countEl.textContent = n;
      }
    };
    const master = $("#check-all");
    if (master) master.addEventListener("change", () => { checks().forEach(c => (c.checked = master.checked)); update(); });
    document.addEventListener("change", e => { if (e.target.classList && e.target.classList.contains("row-check")) update(); });
    // shift-click range select
    let last = null;
    document.addEventListener("click", e => {
      const c = e.target.closest && e.target.closest(".row-check");
      if (!c) return;
      if (e.shiftKey && last && last !== c) {
        const list = checks(), a = list.indexOf(last), b = list.indexOf(c);
        list.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(x => (x.checked = c.checked));
        update();
      }
      last = c;
    });
    const pickAll = $("#pick-all-matching");
    if (pickAll) pickAll.addEventListener("click", e => {
      e.preventDefault();
      selectAllField.value = "1";
      if (countEl) countEl.textContent = pickAll.dataset.total;
      allNote.innerHTML = "All " + pickAll.dataset.total + " matching rows are selected. <a href='#' id='clear-all-matching'>Clear</a>";
      const clr = $("#clear-all-matching");
      clr.addEventListener("click", ev => { ev.preventDefault(); selectAllField.value = "0"; checks().forEach(c => (c.checked = false)); if (master) master.checked = false; update(); });
    });
    // action buttons that need extra inputs
    $$("[data-needs]", bulk).forEach(btn => btn.addEventListener("click", e => {
      const field = $(btn.dataset.needs, bulk);
      if (field && !field.value.trim()) { e.preventDefault(); field.focus(); field.style.outline = "2px solid #f59e0b"; }
    }));
    update();
  }

  /* ---- insert merge field at cursor ---- */
  let lastField = null;
  document.addEventListener("focusin", e => {
    if (e.target.matches("textarea.body-edit, input.subject-edit")) lastField = e.target;
  });
  document.addEventListener("click", e => {
    const chip = e.target.closest(".chip[data-var]");
    if (!chip) return;
    const card = chip.closest(".stage-card, form") || document;
    const target = (lastField && card.contains(lastField)) ? lastField : $("textarea.body-edit", card);
    if (!target) return;
    const tok = "{{" + chip.dataset.var + "}}";
    const s = target.selectionStart || 0, en = target.selectionEnd || 0;
    target.value = target.value.slice(0, s) + tok + target.value.slice(en);
    target.focus();
    target.selectionStart = target.selectionEnd = s + tok.length;
  });

  /* ---- live preview of a stage ---- */
  document.addEventListener("click", async e => {
    const btn = e.target.closest(".js-preview");
    if (!btn) return;
    e.preventDefault();
    const card = btn.closest(".stage-card");
    const out = $(".js-preview-out", card);
    const params = new URLSearchParams();
    const sel = $(".js-sample", card);
    if (sel && sel.value) params.set("contact_id", sel.value);
    const subj = $("input.subject-edit", card), body = $("textarea.body-edit", card);
    if (subj) params.set("subject", subj.value);
    if (body) params.set("body", body.value);
    out.classList.remove("hidden");
    out.textContent = "Loading preview...";
    try {
      const r = await fetch(btn.dataset.url + "?" + params.toString(), { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      out.innerHTML = "";
      const to = document.createElement("div");
      to.className = "small muted"; to.textContent = "To: " + d.to;
      const sj = document.createElement("div");
      sj.className = "subj"; sj.textContent = d.subject || "(no subject)";
      const bd = document.createElement("div");
      bd.textContent = d.body || "(empty body)";
      out.append(to, sj, bd);
    } catch (err) { out.textContent = "Preview failed: " + err.message; }
  });

  /* ---- load a saved template into a stage ---- */
  document.addEventListener("change", async e => {
    const sel = e.target.closest(".js-template");
    if (!sel || !sel.value) return;
    const card = sel.closest(".stage-card");
    try {
      const r = await fetch("/templates/" + sel.value + ".json");
      const d = await r.json();
      const subj = $("input.subject-edit", card), body = $("textarea.body-edit", card);
      if (subj && d.subject) subj.value = d.subject;
      if (body) body.value = d.body;
    } catch (err) { alert("Could not load the template."); }
    sel.value = "";
  });

  /* ---- dialogs ---- */
  document.addEventListener("click", e => {
    const open = e.target.closest("[data-dialog]");
    if (open) { e.preventDefault(); const d = document.getElementById(open.dataset.dialog); if (d && d.showModal) d.showModal(); return; }
    const close = e.target.closest("[data-close]");
    if (close) { const d = close.closest("dialog"); if (d) d.close(); }
    if (e.target.tagName === "DIALOG") e.target.close();
  });

  /* ---- copy buttons ---- */
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy);
    const old = b.textContent; b.textContent = "Copied"; setTimeout(() => (b.textContent = old), 1200);
  });

  /* ---- keep scroll position on stage anchors after redirects ---- */
  if (location.hash) { const t = document.querySelector(location.hash); if (t) t.scrollIntoView({ block: "start" }); }

  /* ---- disable submit buttons after click to avoid double sends ---- */
  document.addEventListener("submit", e => {
    if (e.defaultPrevented) return;
    const b = e.submitter;
    if (b && b.dataset.once) { setTimeout(() => { b.disabled = true; b.textContent = "Working..."; }, 0); }
  });
})();

/* ---- CRM additions ---- */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const csrf = () => ($("meta[name=csrf]") || {}).content || "";

  /* channel-dependent fields in message composers */
  $$("form[data-channel-form]").forEach(form => {
    const sync = () => {
      const picked = ($("input[name=channel]:checked", form) || {}).value || form.dataset.channel || "email";
      $$("[data-for]", form).forEach(el => el.classList.toggle("hidden", el.dataset.for !== picked));
    };
    form.addEventListener("change", e => {
      if (e.target.name === "channel") sync();
      if (e.target.name === "wa_template") {
        const opt = e.target.selectedOptions[0];
        const lang = $("input[name=wa_language]", form);
        if (opt && opt.dataset.lang && lang) lang.value = opt.dataset.lang;
      }
    });
    sync();
  });

  /* kanban drag & drop: POST /deals/<id>/move */
  let dragged = null;
  document.addEventListener("dragstart", e => {
    const card = e.target.closest && e.target.closest(".deal-card");
    if (!card) return;
    dragged = card;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  document.addEventListener("dragend", () => { if (dragged) dragged.classList.remove("dragging"); dragged = null; $$(".lane-body.drop").forEach(l => l.classList.remove("drop")); });
  document.addEventListener("dragover", e => {
    const lane = e.target.closest && e.target.closest(".lane-body");
    if (!lane || !dragged) return;
    e.preventDefault();
    $$(".lane-body.drop").forEach(l => l !== lane && l.classList.remove("drop"));
    lane.classList.add("drop");
  });
  document.addEventListener("drop", async e => {
    const lane = e.target.closest && e.target.closest(".lane-body");
    if (!lane || !dragged) return;
    e.preventDefault();
    lane.classList.remove("drop");
    const card = dragged, from = card.parentElement;
    if (from === lane) return;
    let reason = "";
    if (lane.dataset.kind === "lost") { reason = window.prompt("Why was it lost? (optional)") || ""; }
    lane.prepend(card);
    const body = new URLSearchParams({ stage_id: lane.dataset.stage, csrf_token: csrf(), lost_reason: reason });
    try {
      const r = await fetch(card.dataset.move, { method: "POST", body, headers: { "X-Requested-With": "fetch", "X-CSRF-Token": csrf() } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const refresh = () => location.reload();
      setTimeout(refresh, 250);   // totals per lane are server-computed
    } catch (err) { from.prepend(card); alert("Could not move the deal: " + err.message); }
  });

  /* auto-scroll chat threads to the newest message */
  const thread = $(".thread-body");
  if (thread) thread.scrollTop = thread.scrollHeight;

  /* insert a saved template into a reply box */
  document.addEventListener("change", async e => {
    const sel = e.target.closest(".js-reply-template");
    if (!sel || !sel.value) return;
    const form = sel.closest("form");
    try {
      const r = await fetch("/templates/" + sel.value + ".json");
      const d = await r.json();
      const body = $("textarea[name=body]", form), subj = $("input[name=subject]", form);
      if (d.channel === "whatsapp" && d.meta && d.meta.wa_template) {
        const t = $("select[name=wa_template]", form);
        if (t) { let o = Array.from(t.options).find(x => x.value === d.meta.wa_template); if (!o) { o = new Option(d.meta.wa_template, d.meta.wa_template); t.add(o); } t.value = d.meta.wa_template; }
        const l = $("input[name=wa_language]", form); if (l && d.meta.language) l.value = d.meta.language;
      }
      if (body && d.body) body.value = d.body;
      if (subj && d.subject) subj.value = d.subject;
    } catch (err) { alert("Could not load the template."); }
    sel.value = "";
  });
})();

/* ---- record lookup: <input list=… data-lookup=URL data-target=hiddenId> ---- */
(function () {
  "use strict";
  let timer = null;
  document.addEventListener("input", e => {
    const inp = e.target;
    if (!inp.dataset || !inp.dataset.lookup) return;
    const hidden = document.getElementById(inp.dataset.target);
    const m = inp.value.match(/#(\d+)$/);
    if (hidden) hidden.value = m ? m[1] : "";
    if (m) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const r = await fetch(inp.dataset.lookup + "?q=" + encodeURIComponent(inp.value));
        const rows = await r.json();
        const dl = document.getElementById(inp.getAttribute("list"));
        dl.innerHTML = "";
        rows.forEach(x => { const o = document.createElement("option"); o.value = x.label + (x.email ? " <" + x.email + ">" : "") + " #" + x.id; dl.appendChild(o); });
      } catch (err) { /* lookup is a convenience only */ }
    }, 180);
  });
})();
