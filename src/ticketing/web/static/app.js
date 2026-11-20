// Small enhancements only (D2): dialogs, confirm prompts, auto-submit selects, one-time submit, copy buttons.
// Every form works without this script.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-open]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const d = document.getElementById(btn.dataset.open);
      if (d && d.showModal) { e.preventDefault(); d.showModal(); const f = d.querySelector("input,select,textarea,button"); if (f) f.focus(); }
    });
  });
  document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
  const opened = document.querySelector("dialog[data-autoopen]");
  if (opened && opened.showModal) opened.showModal();
  document.querySelectorAll("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => {
    if (!confirm(f.dataset.confirm)) e.preventDefault();
  }));
  document.querySelectorAll("form[data-once]").forEach((f) => f.addEventListener("submit", () => {
    f.querySelectorAll("button[type=submit]").forEach((b) => { b.disabled = true; b.textContent = "Sending…"; });
  }));
  document.querySelectorAll("select[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
    const el = document.getElementById(b.dataset.copy);
    if (el && navigator.clipboard) navigator.clipboard.writeText(el.textContent.trim()).then(() => { b.textContent = "Copied"; });
  }));
  const typeSel = document.getElementById("type_id");
  if (typeSel) {
    const show = () => { const o = typeSel.selectedOptions[0]; const t = document.getElementById("team-note");
      if (t) t.textContent = o && o.dataset.team ? "Handled by team " + o.dataset.team : ""; };
    typeSel.addEventListener("change", show); show();
  }
});
