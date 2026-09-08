(function bootstrapReaderRouting() {
  function numericRouteId(value) {
    return /^\d+$/.test(String(value || "")) ? Number(value) : null;
  }

  function eventPaneFromRoute(parts) {
    if (parts[2] === "materials") return "timeline";
    if (parts[2] === "differences") return "evidence";
    return ["timeline", "evidence", "related"].includes(parts[2]) ? parts[2] : "overview";
  }

  function countryPaneFromRoute(parts) {
    const legacy = { indicators: "data", issues: "policies", signals: "events" };
    const pane = legacy[parts[2]] || parts[2];
    return ["events", "research", "reports", "policies", "field", "data"].includes(pane) ? pane : "overview";
  }

  function topicPaneFromRoute(parts) {
    const legacy = {
      library: "evidence",
      materials: "evidence",
      activity: "tasks",
      feed: "tasks",
      events: "tasks",
      runs: "tasks",
      "policy-graph": "outputs",
      collaboration: "settings",
    };
    const pane = legacy[parts[2]] || parts[2];
    return ["tasks", "evidence", "outputs", "settings"].includes(pane) ? pane : "overview";
  }

  function projectPaneFromRoute(parts) {
    return topicPaneFromRoute(parts);
  }

  function capabilityPaneFromRoute(parts) {
    return ["overview", "io", "configure", "runs", "review"].includes(parts[2]) ? parts[2] : "overview";
  }

  window.ReaderRouting = Object.freeze({
    numericRouteId,
    eventPaneFromRoute,
    countryPaneFromRoute,
    topicPaneFromRoute,
    projectPaneFromRoute,
    capabilityPaneFromRoute,
  });
}());
