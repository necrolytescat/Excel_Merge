(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) {
    root.MonitorRequestLedger = api.MonitorRequestLedger;
    root.MonitorTaskRefreshPolicy = {
      shouldPauseAutomaticRefresh: api.shouldPauseAutomaticRefresh,
    };
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
      return Object.keys(value).sort().reduce((result, key) => {
        result[key] = canonical(value[key]);
        return result;
      }, {});
    }
    return value;
  }

  function shouldPauseAutomaticRefresh(automatic, loadedCount) {
    return Boolean(automatic && loadedCount > 30);
  }

  class MonitorRequestLedger {
    constructor(options = {}) {
      this.fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
      this.uuidFactory = options.uuidFactory || (() => globalThis.crypto.randomUUID());
      this.pending = new Map();
    }

    key(method, target, schemaVersion, payload) {
      return method.toUpperCase() + " " + target + " " + JSON.stringify(canonical({
        schema_version: schemaVersion,
        ...payload,
      }));
    }

    async send(target, { method, schemaVersion, payload }) {
      const key = this.key(method, target, schemaVersion, payload);
      const requestId = this.pending.get(key) || this.uuidFactory();
      this.pending.set(key, requestId);
      let response;
      try {
        response = await this.fetchImpl(target, {
          method,
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({
            schema_version: schemaVersion,
            request_id: requestId,
            ...payload,
          }),
        });
      } catch (error) {
        throw error;
      }
      let body;
      try {
        body = await response.json();
      } catch (error) {
        throw new Error("版本监控响应体无法确认");
      }
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        throw new Error("版本监控响应格式无法确认");
      }
      this.pending.delete(key);
      if (!response.ok) throw body;
      return {
        body,
        etag: response.headers.get("ETag") || "",
        status: response.status,
      };
    }
  }

  return { MonitorRequestLedger, shouldPauseAutomaticRefresh };
});
