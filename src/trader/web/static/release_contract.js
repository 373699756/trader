(function () {
  "use strict";

  const STATUS_SCHEMA = "runtime_status";
  const FREEZE_VIEW_SCHEMA = "freeze_view";
  const ERROR_CODE = "release_contract_mismatch";

  function statusPayloadCompatibility(payload) {
    if (!payload || payload.schema_version !== STATUS_SCHEMA) {
      return { compatible: false, reason: "status_schema_mismatch" };
    }
    const release = payload.release;
    if (!release || typeof release !== "object") {
      return { compatible: false, reason: "release_identity_missing" };
    }
    if (release.freeze_view_schema !== FREEZE_VIEW_SCHEMA) {
      return { compatible: false, reason: "freeze_schema_mismatch" };
    }
    return { compatible: true, reason: "" };
  }

  function freezePayloadCompatibility(payload) {
    if (!payload || payload.schema_version !== FREEZE_VIEW_SCHEMA) {
      return { compatible: false, reason: "freeze_schema_mismatch" };
    }
    return { compatible: true, reason: "" };
  }

  function mismatchError(reason) {
    const error = new Error("页面资源与后台服务版本不一致");
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
