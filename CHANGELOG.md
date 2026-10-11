# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-12

### Added
- **Multi-Storefront Pricing Engine**: Regional pricing rankings and currency arbitrage spread computation across App Store, Google Play, and web storefronts.
- **First-Party Apple App Store Validator**: Direct validation of in-app purchase tiers against Apple CDN public SSR pages with 301/302 redirect anti-spoofing detection and sub-0.5s deterministic fast-fail.
- **Dynamic Catalog Indexing**: Multi-tier fuzzy catalog search engine with offline JSON fallback and configurable 24h caching.
- **Embedded Web Dashboard SPA (`openrank serve`)**: Zero-dependency luxury dark UI with desktop/mobile responsiveness, real-time search autocomplete, CSV/JSON file exports, and persistent watchlist/settings storage.
- **Zero-Dependency Architecture**: 100% built on the Python standard library with zero runtime dependencies.
- **Terminal UI**: ANSI color-coded delta percentages, box-drawing table formatting, and `NO_COLOR` / `TERM=dumb` compliance.
- **Dual Executable Wrappers**: Standalone `bin/openrank` and `bin/otr` launchers with automated sys.path bootstrap.
- **Complete Test Suite**: 273 hermetic offline unit, integration, and E2E browser tests passing cleanly with zero warnings.
