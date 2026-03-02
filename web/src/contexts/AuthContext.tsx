// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { createContext, useContext } from 'react'
import { AuthStatus } from '../api/client'

export interface AuthContextValue {
  authStatus: AuthStatus | null
  isAdmin: boolean
  canMutate: boolean // admin or editor
  isViewer: boolean
}

const AuthContext = createContext<AuthContextValue>({
  authStatus: null,
  isAdmin: false,
  canMutate: false,
  isViewer: false,
})

export const AuthProvider = AuthContext.Provider

export function useAuth(): AuthContextValue {
  return useContext(AuthContext)
}
