// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { humanize } from '../../utils/audit'

/** A project's alert kinds, in the order their lines show on the card. */
export const PROJECT_KINDS = [
  'connectors_off',
  'pool_no_routable',
  'countries_unserved',
  'vendor_login',
  'vendor_rate_limited',
  'vendor_forbidden',
  'vendor_cannot_serve',
  'vendor_refused_other',
  'connector_failing',
  'refusals',
  'success_rate',
  'connector_sync_failing',
  'connector_cloud_error',
  'connector_no_healthy_proxy',
  'connector_traffic_blocked',
  'connector_traffic_near',
  'connector_traffic_over_alert',
  'pool_unhealthy_share',
  'exits_contradicted',
  'exits_outside_countries',
  'reputation_site_blocked',
  'connector_stuck_deleting',
] as const

/** The install's alert kinds, in the order their lines show on the Projects page. */
export const INSTALL_KINDS = [
  'no_gateway_serving',
  'store_unreachable',
  'changes_not_applied',
  'background_behind',
  'ip_databases',
] as const

/** What each kind of alert is about, in a few words, for the Alerts pages' kind filter and details. */
export const KIND_LABELS: Record<string, string> = {
  connectors_off: 'No connector on',
  pool_no_routable: 'No proxy takes requests',
  countries_unserved: 'Countries nobody serves',
  vendor_login: 'Vendor rejecting the login',
  vendor_rate_limited: 'Vendor rate limiting',
  vendor_forbidden: 'Vendor forbidding sites or ports',
  vendor_cannot_serve: 'Vendor cannot serve',
  vendor_refused_other: 'Vendor refusing requests',
  connector_failing: 'Connector failing requests',
  refusals: 'Requests turned away',
  success_rate: 'Low success rate',
  connector_sync_failing: 'Proxy list sync failing',
  connector_cloud_error: 'Cloud instances failing',
  connector_no_healthy_proxy: 'No healthy proxy',
  connector_traffic_blocked: 'Traffic limit reached',
  connector_traffic_near: 'Traffic limit near',
  connector_traffic_over_alert: 'Over an alert-only traffic limit',
  pool_unhealthy_share: 'Many proxies unhealthy',
  exits_contradicted: 'Exits not where the vendor says',
  exits_outside_countries: 'Exits outside the listed countries',
  reputation_site_blocked: 'Site blocking every connector',
  connector_stuck_deleting: 'Connector stuck deleting',
  no_gateway_serving: 'No gateway serving',
  store_unreachable: 'Data store out of reach',
  changes_not_applied: 'Changes not applied',
  background_behind: 'Background work behind',
  ip_databases: 'IP databases failing',
}

/** A kind's label; a kind this release does not know reads as its name in words. */
export const kindLabel = (kind: string): string =>
  Object.prototype.hasOwnProperty.call(KIND_LABELS, kind) ? KIND_LABELS[kind] : humanize(kind)

/** The kind of the line a key names: `<kind>` or `<kind>:<subject>`. */
export const kindOfKey = (key: string): string => key.split(':', 1)[0]
