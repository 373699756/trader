(function () {
  "use strict";

  const STATUS_SCHEMA = "v2_status_v2";
  const DECISION_VIEW_SCHEMA = "v2_decision_view_v2";
  const WEB_ASSET_REVISION = "release-contract-2026-08-24-v12";
  const ERROR_CODE = "release_contract_mismatch";

  function statusPayloadCompatibility(payload) {
    if (!payload || payload.schema_version !== STATUS_SCHEMA) {
      return { compatible: false, reason: "status_schema_mismatch" };
    }
    const release = payload.release;
    if (!release || typeof release !== "object") {
      return { compatible: false, reason: "release_identity_missing" };
    }
    if (release.decision_view_schema !== DECISION_VIEW_SCHEMA) {
      return { compatible: false, reason: "decision_schema_mismatch" };
    }
    if (release.web_asset_revision !== WEB_ASSET_REVISION) {
      return { compatible: false, reason: "web_asset_revision_mismatch" };
    }
    return { compatible: true, reason: "" };
  }

  function decisionPayloadCompatibility(payload) {
    if (!payload || payload.schema_version !== DECISION_VIEW_SCHEMA) {
      return { compatible: false, reason: "decision_schema_mismatch" };
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
    decisionPayloadCompatibility,
    mismatchError,
    isMismatchError,
  });
})();
