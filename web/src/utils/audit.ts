// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { AUDIT_MASK, type AuditEntityType, type AuditValue } from '../api/client'
import { formatDateTime } from './format'

/** What each kind of entity is called in History and on the Activity page. */
export const ENTITY_LABELS: Record<AuditEntityType, string> = {
  project: 'Project',
  connector: 'Connector',
  credential: 'Credential',
  proxy: 'Proxy',
  wireguard_device: 'WireGuard device',
  openvpn_device: 'OpenVPN device',
  provider: 'Provider',
  user: 'User',
  sign_in_provider: 'Sign-in provider',
  geo_settings: 'IP attribution settings',
  geo_database: 'IP database',
  wireguard_settings: 'WireGuard settings',
  openvpn_settings: 'OpenVPN settings',
  system_settings: 'System settings',
  backup: 'Backup',
}

/** Every action by name: the changes the free edition records, then the sign-in and token events Enterprise adds. */
export const ACTION_LABELS: Record<string, string> = {
  created: 'Created',
  updated: 'Updated',
  deleted: 'Deleted',
  imported: 'Imported',
  traffic_reset: 'Traffic reset',
  muted: 'Muted',
  unmuted: 'Unmuted',
  invited: 'Invited',
  invite_reissued: 'Invite reissued',
  rotated: 'Rotated',
  signed_in: 'Signed in',
  sign_in_failed: 'Sign-in failed',
  token_issued: 'Token issued',
  token_first_used: 'Token first used',
  token_revoked: 'Token revoked',
  identity_unlinked: 'Sign-in unlinked',
}

export type ActionTone = 'green' | 'blue' | 'red' | 'gray'

const ACTION_TONES: Record<string, ActionTone> = {
  created: 'green', imported: 'green', invited: 'green', signed_in: 'green', token_issued: 'green',
  updated: 'blue', traffic_reset: 'blue', invite_reissued: 'blue', rotated: 'blue', unmuted: 'blue',
  deleted: 'red', sign_in_failed: 'red', token_revoked: 'red', identity_unlinked: 'red',
  muted: 'gray',
}

// Words that read as letters, wherever they appear in a field name.
const ACRONYMS: Record<string, string> = {
  api: 'API', asn: 'ASN', ca: 'CA', dns: 'DNS', gb: 'GB', http: 'HTTP', https: 'HTTPS', id: 'ID', ids: 'IDs', ip: 'IP', ips: 'IPs',
  mitm: 'MITM', mtu: 'MTU', pem: 'PEM', ssh: 'SSH', sso: 'SSO', tls: 'TLS', ua: 'UA', uri: 'URI', url: 'URL', vm: 'VM',
}

// Names that read better said differently than spelled out.
const FIELD_NAMES: Record<string, string> = {
  is_active: 'Active',
  tls_mitm_mode: 'TLS interception',
  tls_mitm_engine: 'TLS engine',
  tls_mitm_browser: 'Browser profile',
  client_ip: 'Address',
  user_agent: 'Browser',
  token_name: 'Token',
  // A project's alert settings (under Alerts), in the Alert settings tab's words.
  success_rate: 'Warn about a low success rate',
  success_warning_below: 'Success rate: warning below (%)',
  success_problem_below: 'Success rate: problem below (%)',
  success_min_requests: 'Success rate: only with at least (requests)',
  refusals_min: 'Turned away: only with at least (requests)',
  refusals_warning_percent: 'Turned away: warning from (%)',
  refusals_problem_percent: 'Turned away: problem from (%)',
  invalid_location_min: 'Locations that could not be read: warning from (requests)',
  vendor_min: 'Vendor refusals: only with at least (refused)',
  vendor_login_problem_percent: 'Login rejected (407): problem from (%)',
  vendor_rate_limited_warning_percent: 'Too many requests (429): warning from (%)',
  vendor_rate_limited_problem_percent: 'Too many requests (429): problem from (%)',
  vendor_forbidden_warning_percent: 'Not allowed (403): warning from (%)',
  vendor_forbidden_problem_percent: 'Not allowed (403): problem from (%)',
  vendor_cannot_serve_warning_percent: 'Cannot serve (400, 502, 503): warning from (%)',
  vendor_other_warning_percent: 'Any other refusal: warning from (%)',
  connector_failing_min_requests: 'Failing requests: only with at least (requests)',
  connector_failing_warning_percent: 'Failing requests: warning from (%)',
  connector_failing_problem_percent: 'Failing requests: problem from (%)',
  connector_errors_min: 'Sync or cloud errors: show after (in a row)',
  stuck_deleting_minutes: 'Still deleting: show after (minutes)',
  unhealthy_share_percent: 'Unhealthy proxies: from (%)',
  unhealthy_min: 'Unhealthy proxies: and at least (proxies)',
  exits_outside_percent: 'Exits outside the listed countries: from (%)',
  exits_outside_min: 'Exits outside the listed countries: and at least (proxies)',
  exits_contradicted_percent: 'Exits not where the vendor says: from (%)',
  exits_contradicted_min: 'Exits not where the vendor says: and at least (proxies)',
  site_blocked_percent: 'A site blocking every connector: from (%)',
  keep_days: 'Keep resolved alerts (days)',
  install_alert_retention_days: 'Keep install alerts (days)',
}

// The settings recorded one level down, by the group they show under. Provider fields (`config`) and a
// descriptor's document (`spec`) read as the entity's own fields.
const FIELD_GROUPS: Record<string, string | null> = {
  config: null,
  spec: null,
  routing_config: 'Routing',
  rate_limit_config: 'Rate limit',
  traffic_config: 'Traffic',
  health_check_config: 'Health check',
  alerts: 'Alerts',
}

// A table's own entry for a key, never one every object inherits: names come from data (a descriptor's field
// keys), and `constructor` or `toString` must read as words.
const own = <T>(table: Record<string, T>, key: string): T | undefined =>
  Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined

/** A snake_case name in words: `client_mtu` is "Client MTU". */
export function humanize(name: string): string {
  const named = own(FIELD_NAMES, name)
  if (named) return named
  const words = name.split('_').filter(Boolean).map((w) => own(ACRONYMS, w.toLowerCase()) ?? w.toLowerCase())
  if (words.length === 0) return name
  const [first, ...rest] = words
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(' ')
}

export function entityLabel(type: string): string {
  return own(ENTITY_LABELS, type) ?? humanize(type)
}

export function actionLabel(action: string): string {
  return own(ACTION_LABELS, action) ?? humanize(action)
}

export function actionTone(action: string): ActionTone {
  return own(ACTION_TONES, action) ?? 'gray'
}

/** A recorded field as people read it: `routing_config.weight` is Weight under Routing; `config.api_key` is API key. */
export function fieldLabel(path: string): { group: string | null; name: string } {
  const dot = path.indexOf('.')
  if (dot < 0) return { group: null, name: humanize(path) }
  const head = path.slice(0, dot)
  const group = own(FIELD_GROUPS, head)
  return { group: group === undefined ? humanize(head) : group, name: humanize(path.slice(dot + 1)) }
}

/** A secret's stand-in: the value it held is never recorded, only whether it was set. */
export const isMasked = (value: AuditValue): boolean => value === AUDIT_MASK

// An API timestamp ("2026-11-10T14:03:22.123456"), shown in the viewer's time like every other.
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/

/** A recorded value as text: lists joined, yes and no for flags, big numbers grouped (not ports), times in the viewer's zone. */
export function formatAuditValue(value: AuditValue): string {
  if (value === null) return ''
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Math.abs(value) >= 100_000 ? value.toLocaleString() : String(value)
  if (typeof value === 'string') return TIMESTAMP.test(value) ? formatDateTime(value) : value
  if (Array.isArray(value)) return value.length ? value.map(formatAuditValue).join(', ') : 'none'
  return JSON.stringify(value)
}
