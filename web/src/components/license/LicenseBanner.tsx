// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Link, useLocation, useNavigate } from 'react-router'
import { AlertTriangle } from 'lucide-react'
import { useAuth } from '../../contexts/AuthContext'
import { useNavigationGuardContext } from '../../contexts/NavigationGuardContext'
import { useLicense } from '../../hooks/useEdition'
import { setSettingsOrigin } from '../../utils/settingsOrigin'

// Where the License card is.
const LICENSE_PAGE = '/admin/system'

/**
 * A bar across the top of every page for admins while the license needs one: its grace period, its expiry, a
 * refused key or more users than seats. The api words it; nothing is ever blocked.
 */
export function LicenseBanner() {
  const { isAdmin } = useAuth()
  const { data: license } = useLicense()
  const location = useLocation()
  const navigate = useNavigate()
  const guard = useNavigationGuardContext()
  if (!isAdmin || !license?.banner) return null
  return (
    <div role="status" className="flex-none flex items-center justify-center gap-2 px-4 py-1.5 border-b border-warning/30 bg-warning-soft text-warning text-[13px]">
      <AlertTriangle className="w-3.5 h-3.5 flex-none" />
      <p className="min-w-0 wrap-break-word">
        {license.banner}
        {location.pathname !== LICENSE_PAGE && (
          <>
            {' '}
            <Link
              to={LICENSE_PAGE}
              className="font-medium underline whitespace-nowrap"
              onClick={(e) => {
                // Back on the System page returns here, and a form with unsaved changes asks first.
                setSettingsOrigin(location.pathname)
                if (!guard.blocked) return
                e.preventDefault()
                guard.request(() => navigate(LICENSE_PAGE))
              }}
            >
              Open the license
            </Link>
          </>
        )}
      </p>
    </div>
  )
}
