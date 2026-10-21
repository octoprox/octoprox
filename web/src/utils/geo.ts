// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { GeoDatabase } from '../api/client'

/**
 * Whether a database is open where requests are judged: on at least one live
 * gateway, as their heartbeats report it. While no gateway reports (none is
 * running yet, or their heartbeats cannot be read) the api process that
 * answered stands in, since every process opens the same files.
 */
export const isInUse = (db: GeoDatabase): boolean =>
  db.enabled && (db.gateways_total > 0 ? db.gateways_loaded > 0 : db.loaded_here)
