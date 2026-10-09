(function () {
  "use strict";

  const { runtimeErrorRows, unassignedIssues } = window.TraderStatusHealth;

  function createErrorDrawer(els, beforeOpen, onVisibilityChange) {
    let issues = [];
    let returnFocus = null;
    const notify = () => {
      if (typeof onVisibilityChange === "function") onVisibilityChange();
    };
    const close = (restoreFocus) => {
      const wasOpen = els.observationDrawer.classList.contains("is-open");
      els.observationDrawer.classList.remove("is-open");
      els.observationDrawer.setAttribute("aria-hidden", "true");
      els.errorDetailsButton.setAttribute("aria-expanded", "false");
      notify();
      if (wasOpen && restoreFocus && returnFocus && typeof returnFocus.focus === "function") returnFocus.focus();
      returnFocus = null;
    };
    const open = () => {
      if (els.errorDetailsButton.disabled) return;
      if (typeof beforeOpen === "function") beforeOpen();
      returnFocus = document.activeElement;
      if (els.observationErrorContent) els.observationErrorContent.innerHTML = runtimeErrorRows(unassignedIssues(issues));
      els.observationDrawer.classList.add("is-open");
      els.observationDrawer.setAttribute("aria-hidden", "false");
      els.errorDetailsButton.setAttribute("aria-expanded", "true");
      notify();
      els.observationDrawerClose.focus();
    };
    els.errorDetailsButton.addEventListener("click", open);
    els.observationDrawerClose.addEventListener("click", () => close(true));
    els.observationErrorContent.addEventListener("click", copyRuntimeCode);
    return {
      close,
      isOpen: () => els.observationDrawer.classList.contains("is-open"),
      setIssues: (nextIssues) => {
        issues = Array.isArray(nextIssues) ? nextIssues : [];
        if (els.observationErrorCount) els.observationErrorCount.textContent = String(unassignedIssues(issues).filter((issue) => issue.recoveryStatus !== "recovered").length);
        if (!els.observationDrawer.classList.contains("is-open")) return;
        els.observationErrorContent.innerHTML = runtimeErrorRows(unassignedIssues(issues));
      },
    };
  }

  async function copyRuntimeCode(event) {
    const button = event.target.closest("button[data-copy-code]");
    if (!button) return;
    const code = button.dataset.copyCode || "";
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(code);
      } else {
        copyTextFallback(code);
      }
      button.textContent = "已复制";
    } catch (_error) {
      selectRuntimeCode(button.previousElementSibling);
      button.textContent = "已选中，请复制";
    }
  }

  function copyTextFallback(value) {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    const copied = document.execCommand("copy");
    input.remove();
    if (!copied) throw new Error("copy_unavailable");
  }

  function selectRuntimeCode(codeElement) {
    if (!codeElement || typeof document.createRange !== "function" || typeof window.getSelection !== "function") return;
    const range = document.createRange();
    range.selectNodeContents(codeElement);
    const selectionRange = window.getSelection();
    if (!selectionRange) return;
    selectionRange.removeAllRanges();
    selectionRange.addRange(range);
  }

  window.TraderErrorDrawer = Object.freeze({ createErrorDrawer });
})();
