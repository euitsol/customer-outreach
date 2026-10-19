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
      allNote.innerHTML = "All " + pickAll.dataset.total + " matching contacts are selected. <a href='#' id='clear-all-matching'>Clear</a>";
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
