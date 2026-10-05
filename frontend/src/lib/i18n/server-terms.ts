// Terms the API sends as data and the interface translates with t(value) (objective presets, time
// profiles…). Listed here so `npm run i18n:check` knows they are used.
export const SERVER_TERMS = ["OTIF First", "Minimize Setup", "Max Throughput", "Minimize Inventory", "Stable Plan", "Balanced", "Quick", "Normal", "Deep",
  // planning run steps
  "Load scenario", "Validate master data", "Validate constraints", "Explode BOM", "Calculate material availability", "Generate operations", "Generate feasible resource alternatives", "Build calendars", "Build setup matrices", "Identify bottlenecks", "Generate initial feasible schedule", "Optimize", "Repair violations", "Calculate KPIs", "Generate explanations", "Save schedule", "Publish result", "Generating initial plan", "Optimizing",
] as const;
