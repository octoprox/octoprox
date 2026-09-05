// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { ProviderSummary } from '../api/client'

// Bundled logos for the shipped provider types. Custom descriptors carry their
// own logo as a data URI; anything without one gets an initials badge.
import awsLogo from '../assets/logos/aws.svg'
import awsLogoDark from '../assets/logos/aws_dark.svg'
import gcpLogo from '../assets/logos/gcp.svg'
import gcpLogoDark from '../assets/logos/gcp_dark.svg'
import azureLogo from '../assets/logos/azure.svg'
import azureLogoDark from '../assets/logos/azure_dark.svg'
import staticProxyLogo from '../assets/logos/static-proxy.svg'
import oxylabsLogo from '../assets/logos/oxylabs.svg'
import brightdataLogo from '../assets/logos/brightdata.svg'
import decodoLogo from '../assets/logos/decodo.svg'
import decodoLogoDark from '../assets/logos/decodo_dark.svg'
import webshareLogo from '../assets/logos/webshare.svg'
import webshareLogoDark from '../assets/logos/webshare_dark.svg'
import iproyalLogo from '../assets/logos/iproyal.svg'
import iproyalLogoDark from '../assets/logos/iproyal_dark.svg'
import netnutLogo from '../assets/logos/netnut.svg'
import netnutLogoDark from '../assets/logos/netnut_dark.svg'

const BUNDLED_LOGOS: Record<string, { light: string; dark: string }> = {
  static_proxy_provider: { light: staticProxyLogo, dark: staticProxyLogo },
  aws: { light: awsLogo, dark: awsLogoDark },
  gcp: { light: gcpLogo, dark: gcpLogoDark },
  azure: { light: azureLogo, dark: azureLogoDark },
  oxylabs: { light: oxylabsLogo, dark: oxylabsLogo },
  brightdata: { light: brightdataLogo, dark: brightdataLogo },
  decodo: { light: decodoLogo, dark: decodoLogoDark },
  webshare: { light: webshareLogo, dark: webshareLogoDark },
  iproyal: { light: iproyalLogo, dark: iproyalLogoDark },
  netnut: { light: netnutLogo, dark: netnutLogoDark },
}

export function providerLogo(provider: ProviderSummary | undefined, type: string | null | undefined, isDark: boolean): string | null {
  if (provider?.logo) return provider.logo
  const bundled = type ? BUNDLED_LOGOS[type] : undefined
  if (bundled) return isDark ? bundled.dark : bundled.light
  return null
}

/** Two-letter badge text for providers without a logo. */
export function providerInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}

export const SECRET_FIELD_KEYS = ['password', 'secret_key', 'client_secret', 'service_account_json', 'token', 'api_key', 'api_token']
