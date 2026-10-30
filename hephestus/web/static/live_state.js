(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.HephestusLiveState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  class LiveEventCursor {
    constructor() {
      this.runId = null;
      this.lastSequence = 0;
      this.seen = new Set();
    }

    observeRun(runId) {
      if (!runId || runId === this.runId) return;
      this.runId = runId;
      this.lastSequence = 0;
      this.seen.clear();
    }

    accept(runId, entry) {
      this.observeRun(runId);
      const sequence = Number(entry?.sequence);
      if (!Number.isInteger(sequence) || sequence < 1) return false;
      const key = `${this.runId || runId || "current"}:${sequence}`;
      if (this.seen.has(key)) return false;
      this.seen.add(key);
      this.lastSequence = Math.max(this.lastSequence, sequence);
      return true;
    }
  }

  return { LiveEventCursor };
});
