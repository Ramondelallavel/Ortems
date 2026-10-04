// Where an alert leads: every alert carries the context of the entity it is about.

export type AlertRow = { id: string; type?: string; source?: string; context?: Record<string, any> | null };

export function alertHref(a: AlertRow): string {
  const c = a.context || {};
  switch (c.page) {
    case "order":
      return c.order_number ? `/planning/orders?q=${encodeURIComponent(c.order_number)}` : "/planning/orders";
    case "orders":
      return c.filter?.status ? `/planning/orders?status=${encodeURIComponent(c.filter.status)}` : "/planning/orders";
    case "capacity":
      return c.resource_code ? `/planning/capacity?resource=${encodeURIComponent(c.resource_code)}` : "/planning/capacity";
    case "materials":
      return c.material ? `/planning/materials?material=${encodeURIComponent(c.material)}` : "/planning/materials";
    case "resources":
      return "/shopfloor/supervisor";
    case "planning":
      return "/planning";
  }
  if (c.order_number) return `/planning/orders?q=${encodeURIComponent(c.order_number)}`;
  if (a.type?.startsWith("MATERIAL")) return "/planning/materials";
  if (c.resource_id) return "/planning/capacity";
  if (a.source === "PLAN") return "/planning";
  return "/planning/alerts";
}
