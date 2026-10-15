// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Proxy URLs and client snippets for the Connect page. Pure, so the page only
 * renders them. The proxy listener speaks plain HTTP (forwarding and CONNECT)
 * and authenticates with Basic auth, so every snippet is an http:// proxy with
 * the project's username, routing parameters included, and password.
 */

export interface ProxyAccess {
  host: string
  port: number
  username: string
  password: string
  /** The project intercepts TLS, so clients must trust octoprox-ca.crt. */
  mitm: boolean
}

export const CA_FILE = 'octoprox-ca.crt'

/** The host as it goes into a URL: an IPv6 literal needs brackets. */
export const urlHost = (host: string) => (host.includes(':') && !host.startsWith('[') ? `[${host}]` : host)

/** `http://host:port`, without credentials. */
export const proxyServer = (a: ProxyAccess) => `http://${urlHost(a.host)}:${a.port}`

/**
 * `http://user:pass@host:port`. The username and password are percent-encoded,
 * since a password with `@`, `:` or `/` would otherwise end the userinfo early;
 * curl, requests, undici and the rest decode them again before sending.
 */
export const proxyUrl = (a: ProxyAccess) =>
  `http://${encodeURIComponent(a.username)}:${encodeURIComponent(a.password)}@${urlHost(a.host)}:${a.port}`

/** The `host:port:username:password` line proxy lists and many scraping tools import. */
export const proxyLine = (a: ProxyAccess) => `${a.host}:${a.port}:${a.username}:${a.password}`

/** Whether the URL form had to percent-encode anything (worth a note under it). */
export const needsEncoding = (a: ProxyAccess) =>
  encodeURIComponent(a.username) !== a.username || encodeURIComponent(a.password) !== a.password

const sh = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`
// A JSON string is a valid Python and JavaScript string literal.
const lit = (s: string) => JSON.stringify(s)

export type SnippetKind = 'curl' | 'python' | 'node' | 'playwright' | 'env'

export const SNIPPETS: { id: SnippetKind; label: string }[] = [
  { id: 'curl', label: 'cURL' },
  { id: 'python', label: 'Python' },
  { id: 'node', label: 'Node.js' },
  { id: 'playwright', label: 'Playwright' },
  { id: 'env', label: 'Environment' },
]

/** A ready-to-run example fetching `target` through the proxy. */
export function snippet(kind: SnippetKind, a: ProxyAccess, target: string): string {
  switch (kind) {
    case 'curl':
      return [
        `curl --proxy ${sh(proxyServer(a))} \\`,
        `  --proxy-user ${sh(`${a.username}:${a.password}`)} \\`,
        ...(a.mitm ? [`  --cacert ${CA_FILE} \\`] : []),
        `  ${sh(target)}`,
      ].join('\n')
    case 'python':
      return [
        `import requests`,
        ``,
        `proxy = ${lit(proxyUrl(a))}`,
        `proxies = {"http": proxy, "https": proxy}`,
        ``,
        `response = requests.get(${lit(target)}, proxies=proxies, ${a.mitm ? `verify=${lit(CA_FILE)}, ` : ''}timeout=30)`,
        `print(response.text)`,
      ].join('\n')
    case 'node':
      return [
        `// npm install undici`,
        ...(a.mitm ? [`import { readFileSync } from 'node:fs'`] : []),
        `import { ProxyAgent, fetch } from 'undici'`,
        ``,
        a.mitm
          ? `const dispatcher = new ProxyAgent({ uri: ${lit(proxyUrl(a))}, requestTls: { ca: readFileSync(${lit(CA_FILE)}) } })`
          : `const dispatcher = new ProxyAgent(${lit(proxyUrl(a))})`,
        `const response = await fetch(${lit(target)}, { dispatcher })`,
        `console.log(await response.text())`,
      ].join('\n')
    case 'playwright':
      return [
        `from playwright.sync_api import sync_playwright`,
        ``,
        `with sync_playwright() as p:`,
        `    browser = p.chromium.launch(proxy={`,
        `        "server": ${lit(proxyServer(a))},`,
        `        "username": ${lit(a.username)},`,
        `        "password": ${lit(a.password)},`,
        `    })`,
        ...(a.mitm
          ? [
            `    # TLS interception is on: skip certificate checks, or install ${CA_FILE} in the system trust store instead.`,
            `    page = browser.new_page(ignore_https_errors=True)`,
          ]
          : [`    page = browser.new_page()`]),
        `    page.goto(${lit(target)})`,
        `    print(page.inner_text("body"))`,
        `    browser.close()`,
      ].join('\n')
    case 'env':
      return [
        `export https_proxy=${sh(proxyUrl(a))}`,
        `export http_proxy="$https_proxy"`,
        ...(a.mitm ? [`# TLS interception is on: point clients at ${CA_FILE} (curl --cacert, REQUESTS_CA_BUNDLE, NODE_EXTRA_CA_CERTS)`] : []),
        `# curl, wget, Python requests and httpx, Go and most CLIs pick these up`,
        `curl ${a.mitm ? `--cacert ${CA_FILE} ` : ''}${sh(target)}`,
      ].join('\n')
  }
}

/** A short random session ID: letters and digits only, so it can never contain a delimiter. */
export function randomSessionId(): string {
  const bytes = new Uint8Array(6)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => (b % 36).toString(36)).join('')
}

/** ISO 3166-1 alpha-2 codes, plus XK (Kosovo), which vendors and IP databases use. */
export const COUNTRY_CODES = (
  'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ ' +
  'BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR ' +
  'CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR ' +
  'GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ' +
  'ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ ' +
  'LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ ' +
  'MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF ' +
  'PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI ' +
  'SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR ' +
  'TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW'
).split(' ')

let regionNames: Intl.DisplayNames | null | undefined
/** English name for a country code, falling back to the code itself. */
export function countryLabel(code: string): string {
  if (regionNames === undefined) {
    try { regionNames = new Intl.DisplayNames(['en'], { type: 'region' }) } catch { regionNames = null }
  }
  return regionNames?.of(code) ?? code
}

/** The US states and DC, the subdivisions every vendor names. */
export const US_STATES: [string, string][] = [
  ['AL', 'Alabama'], ['AK', 'Alaska'], ['AZ', 'Arizona'], ['AR', 'Arkansas'], ['CA', 'California'],
  ['CO', 'Colorado'], ['CT', 'Connecticut'], ['DE', 'Delaware'], ['DC', 'District of Columbia'],
  ['FL', 'Florida'], ['GA', 'Georgia'], ['HI', 'Hawaii'], ['ID', 'Idaho'], ['IL', 'Illinois'],
  ['IN', 'Indiana'], ['IA', 'Iowa'], ['KS', 'Kansas'], ['KY', 'Kentucky'], ['LA', 'Louisiana'],
  ['ME', 'Maine'], ['MD', 'Maryland'], ['MA', 'Massachusetts'], ['MI', 'Michigan'], ['MN', 'Minnesota'],
  ['MS', 'Mississippi'], ['MO', 'Missouri'], ['MT', 'Montana'], ['NE', 'Nebraska'], ['NV', 'Nevada'],
  ['NH', 'New Hampshire'], ['NJ', 'New Jersey'], ['NM', 'New Mexico'], ['NY', 'New York'],
  ['NC', 'North Carolina'], ['ND', 'North Dakota'], ['OH', 'Ohio'], ['OK', 'Oklahoma'], ['OR', 'Oregon'],
  ['PA', 'Pennsylvania'], ['RI', 'Rhode Island'], ['SC', 'South Carolina'], ['SD', 'South Dakota'],
  ['TN', 'Tennessee'], ['TX', 'Texas'], ['UT', 'Utah'], ['VT', 'Vermont'], ['VA', 'Virginia'],
  ['WA', 'Washington'], ['WV', 'West Virginia'], ['WI', 'Wisconsin'], ['WY', 'Wyoming'],
]
