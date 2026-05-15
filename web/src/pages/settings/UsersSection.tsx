// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import UsersPage from '../../components/UsersPage'

// Thin wrapper so the settings route tree has a section component matching
// the Account/Appearance pattern. UsersPage detects /settings/* and renders
// in embedded mode (no chrome).
export default function UsersSection() {
  return <UsersPage />
}
