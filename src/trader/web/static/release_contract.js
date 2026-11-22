(function () {
  "use strict";

  const ERROR_CODE = "release_contract_mismatch";

  function statusPayloadCompatibility(payload) {
    if (!payload || typeof payload.status !== "string"
      || typeof payload.runtime_started !== "boolean"
      || typeof payload.runtime_version !== "string" || !payload.runtime_version
      || !payload.strategies || typeof payload.strategies !== "object"
      || Array.isArray(payload.strategies)) {
      return { compatible: false, reason: "status_shape_invalid" };
    }
    return { compatible: true, reason: "" };
  }

  function freezePayloadCompatibility(payload) {
    if (!payload || !["tomorrow", "d25", "long"].includes(payload.strategy)
      || !["ready", "not_ready", "not_applicable"].includes(payload.status)
      || !Array.isArray(payload.items)) {
      return { compatible: false, reason: "freeze_shape_invalid" };
    }
    const validView = payload.strategy === "long"
      ? payload.view === "current" && payload.score_status === "not_applicable" && !Object.hasOwn(payload, "frozen")
      : ["current", "live", "official", "history"].includes(payload.view) && typeof payload.frozen === "boolean";
    return validView ? { compatible: true, reason: "" } : { compatible: false, reason: "freeze_shape_invalid" };
  }

  function mismatchError(reason) {
    const error = new Error("后台数据结构与页面要求不一致");
    error.code = ERROR_CODE;
    error.reason = reason || "release_identity_missing";
    return error;
  }

  function isMismatchError(error) {
    return Boolean(error && error.code === ERROR_CODE);
  }

  window.TraderReleaseContract = Object.freeze({
    statusPayloadCompatibility,
    freezePayloadCompatibility,
    mismatchError,
    isMismatchError,
  });
})();
