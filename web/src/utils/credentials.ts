// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { CredentialType } from '../api/client'

// Import logos
import awsLogo from '../assets/logos/aws.svg'
import awsLogoDark from '../assets/logos/aws_dark.svg'
import gcpLogo from '../assets/logos/gcp.svg'
import gcpLogoDark from '../assets/logos/gcp_dark.svg'
import azureLogo from '../assets/logos/azure.svg'
import azureLogoDark from '../assets/logos/azure_dark.svg'
import staticProxyLogo from '../assets/logos/static-proxy.svg'
import oxylabsLogo from '../assets/logos/oxylabs.svg'
import brightdataLogo from '../assets/logos/brightdata.svg'

export const CREDENTIAL_TYPES: { value: CredentialType; label: string; name: string; description: string; logo: string; logoDark: string }[] = [
  { value: 'static_proxy_provider', label: 'Static Proxy Provider', name: 'Static Proxy Provider', description: 'Manually managed proxy servers', logo: staticProxyLogo, logoDark: staticProxyLogo },
  { value: 'aws', label: 'AWS', name: 'Amazon Web Services', description: 'EC2 instances as proxy servers', logo: awsLogo, logoDark: awsLogoDark },
  { value: 'gcp', label: 'GCP', name: 'Google Cloud Platform', description: 'Compute Engine VMs as proxy servers', logo: gcpLogo, logoDark: gcpLogoDark },
  { value: 'azure', label: 'Azure', name: 'Microsoft Azure', description: 'Virtual Machines as proxy servers', logo: azureLogo, logoDark: azureLogoDark },
  { value: 'oxylabs', label: 'Oxylabs', name: 'Oxylabs', description: 'Residential, Mobile, ISP, and Datacenter proxies', logo: oxylabsLogo, logoDark: oxylabsLogo },
  { value: 'brightdata', label: 'BrightData', name: 'BrightData', description: 'Residential, Mobile, ISP, and Datacenter proxies', logo: brightdataLogo, logoDark: brightdataLogo },
]
