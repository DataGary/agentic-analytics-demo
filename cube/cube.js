// Cube enforces the same policy as the MCP server (config/policies.yaml, mounted read-only).
// This is the second layer: even if a caller skips the MCP server and hits Cube directly
// with a valid token, the scope and row rules still apply.
//
// JWT verification itself is built in: Cube checks the HS256 signature with CUBEJS_API_SECRET and the
// audience with CUBEJS_JWT_AUDIENCE, then exposes the claims as securityContext.

const fs = require('fs');
const yaml = require('js-yaml');

const POLICIES = yaml.load(fs.readFileSync('/cube/conf/policies.yaml', 'utf8'));
const GRANULARITY_ORDER = ['day', 'week', 'month', 'quarter', 'year'];

function referencedMembers(query) {
  const out = new Set();
  (query.measures || []).forEach((m) => out.add(m));
  (query.dimensions || []).forEach((d) => out.add(d));
  (query.segments || []).forEach((s) => out.add(s));
  (query.timeDimensions || []).forEach((t) => out.add(t.dimension));
  const walk = (filters) =>
    (filters || []).forEach((f) => {
      if (f.or) walk(f.or);
      else if (f.and) walk(f.and);
      else out.add(f.member || f.dimension);
    });
  walk(query.filters);
  const order = query.order || {};
  (Array.isArray(order) ? order.map((o) => o[0]) : Object.keys(order)).forEach((k) => out.add(k));
  return [...out];
}

module.exports = {
  queryRewrite: (query, { securityContext }) => {
    const role = securityContext && securityContext.role;
    const policy = role && POLICIES.roles[role];
    if (!policy) throw new Error('No valid role in security context');

    if (!policy.members.includes('*')) {
      if (query.ungrouped) throw new Error(`Row-level (ungrouped) queries are not permitted for role ${role}`);
      if (!query.measures || query.measures.length === 0)
        throw new Error(`Role ${role} may only run aggregated queries (at least one measure)`);
      const denied = referencedMembers(query).filter((m) => !policy.members.includes(m));
      if (denied.length) throw new Error(`Members not permitted for role ${role}: ${denied.join(', ')}`);
    }

    if (policy.min_granularity) {
      const min = GRANULARITY_ORDER.indexOf(policy.min_granularity);
      for (const td of query.timeDimensions || []) {
        if (td.granularity && GRANULARITY_ORDER.indexOf(td.granularity) < min)
          throw new Error(`Granularity finer than ${policy.min_granularity} is not permitted for role ${role}`);
      }
    }

    if (policy.row_scope === 'region') {
      if (securityContext.region_id == null) throw new Error('Region-scoped role without region_id');
      query.filters = [
        ...(query.filters || []),
        { member: 'regions.id', operator: 'equals', values: [String(securityContext.region_id)] },
      ];
    }
    return query;
  },
};
