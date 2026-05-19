import { createElement, Fragment } from 'react'
import { defineConfig, McpSource } from 'vocs/config'

import { sidebar } from './sidebar.js'

const basePath = process.env.VOCS_BASE_PATH || undefined
// Outer-scope identifiers (constants, helper functions) referenced from
// inside Vocs config callbacks like `ogImageUrl` and `head` are NOT
// captured by Vocs's config-serializer — it stringifies the function
// body and re-evaluates it in a scope where the outer identifier doesn't
// exist, throwing `ReferenceError: siteUrl is not defined` at SSR time
// and producing a blank page in dev. Anything those callbacks need has
// to be inlined into the callback body. SITE_URL kept here as a single
// source of truth for the `baseUrl` field only.
const SITE_URL = 'https://centaur.run'

export default defineConfig({
  rootDir: '.',
  srcDir: '.',
  renderStrategy: 'full-static',
  // The dead-link checker doesn't know about static assets shipped via
  // public/ (like our zip and brand SVGs), so downgrade to a warning rather
  // than failing the build.
  checkDeadlinks: 'warn',
  baseUrl: SITE_URL,
  title: 'Centaur',
  titleTemplate: '%s - Centaur',
  description: 'The production control plane for shared AI agents, tools, workflows, and sandboxes.',
  // Browser-tab favicon: standalone centaur mark only (no background frame).
  // Vocs emits a per-scheme <link rel="icon"> pair so the tab shows the
  // black silhouette on light chrome and the white silhouette on dark.
  iconUrl: {
    light: '/brand/mark-black.svg',
    dark: '/brand/mark-white.svg',
  },
  // Top-left site logo: full lockup on docs routes (the sidebar / topNav
  // gets enough space to carry the wordmark). The landing page hides Vocs's
  // topNav entirely and renders its own lockup in the hero.
  logoUrl: {
    light: '/brand/lockup-black.svg',
    dark: '/brand/lockup-white.svg',
  },
  mcp: {
    enabled: true,
    sources: [
      McpSource.github({
        name: 'centaur',
        repo: 'paradigmxyz/centaur',
        paths: ['docs', 'services', 'centaur_sdk', 'packages', 'tools', 'workflows'],
      }),
    ],
  },
  // Body copy uses Amp's PolySans via the pages/_root.css override. Docs headings
  // use Perfectly Nineties, while the landing hero uses Sagittaire Display.
  // Code blocks stay on Geist Mono.
  font: {
    mono: { google: 'Geist Mono' },
  },
  // Open Graph cards are pre-rendered at build time by scripts/build-og.ts.
  // Resolve them via a function so the slug logic stays in lockstep with
  // the build script: route /a/b -> /og/a_b.png, / -> /og/index.png. The
  // absolute URL form (baseUrl prefix) is required for Slack/Twitter/
  // Discord previews to resolve the image — relative paths fail in
  // every major unfurl previewer. New routes added under pages/ (e.g.
  // operate/slack-etl) get picked up automatically the next time the
  // prebuild script runs — no manual map maintenance required.
  ogImageUrl: (path: string, { baseUrl }: { baseUrl: string }) => {
    // siteUrl inlined here because Vocs's config-serializer drops the
    // outer-scope binding when it re-evaluates this callback at SSR.
    const siteUrl = 'https://centaur.run'
    const key = path.replace(/\/$/, '') || '/'
    const slug = key === '/' ? 'index' : key.replace(/^\//, '').replace(/\//g, '_')
    const root = baseUrl ?? siteUrl
    return `${root.replace(/\/$/, '')}/og/${slug}.png`
  },
  ...(basePath ? { basePath } : {}),
  editLink: {
    pattern: 'https://github.com/paradigmxyz/centaur/edit/main/docs/pages/:path',
    text: 'Edit this page',
  },
  // Per-page <head>: canonical URL for SEO plus the global font preload and
  // the centaur-brand-menu.js script that powers the right-click logo menu.
  head({ path }) {
    // Helpers + constants inlined here because Vocs's config-serializer
    // re-evaluates this callback in a scope without the outer bindings.
    const siteUrl = 'https://centaur.run'
    const canonicalHref =
      path === '/' ? `${siteUrl}/` : `${siteUrl}${path.replace(/\/+$/, '')}/`
    return createElement(Fragment, null,
      createElement('link', { rel: 'canonical', href: canonicalHref }),
      createElement('link', {
        rel: 'preload',
        href: '/fonts/PerfectlyNineties-Regular.woff',
        as: 'font',
        type: 'font/woff',
        crossOrigin: '',
      }),
      createElement('link', {
        rel: 'preload',
        href: '/fonts/PolySans-variable.woff2',
        as: 'font',
        type: 'font/woff2',
        crossOrigin: '',
      }),
      createElement('link', {
        rel: 'preload',
        href: '/fonts/slack/lato-latin-400-normal.woff2',
        as: 'font',
        type: 'font/woff2',
        crossOrigin: '',
      }),
      createElement('script', { src: '/centaur-brand-menu.js', defer: true }),
    )
  },
  llms: {
    generateMarkdown: true,
  },
  markdown: {
    code: {
      themes: {
        dark: 'github-dark-default',
        light: 'github-dark-default',
      },
    },
  },
  topNav: [
    {
      text: 'Docs',
      link: '/what-is-centaur',
      match: '/what-is-centaur',
    },
    {
      text: 'GitHub',
      link: 'https://github.com/paradigmxyz/centaur',
    },
  ],
  search: {
    boostDocument(documentId) {
      if (documentId.includes('what-is-centaur')) return 4.5
      if (documentId.includes('quickstart')) return 4
      if (documentId.includes('operate/')) return 3.8
      if (documentId.includes('extend/')) return 3.8
      if (documentId.includes('secrets/')) return 3.8
      if (documentId.includes('security')) return 3.6
      if (documentId.includes('deploying-in-production')) return 3.5
      if (documentId.includes('architecture')) return 3
      return 1
    },
  },
  sidebar,
  theme: {
    accentColor: {
      light: '#00E100',
      dark: '#00E100',
    },
    colorScheme: 'dark',
    variables: {
      color: {
        background: {
          light: '#ffffff',
          dark: '#050505',
        },
        text: {
          light: '#050505',
          dark: '#f7f7f2',
        },
      },
      content: {
        width: '920px',
      },
    },
  },
})
