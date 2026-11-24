# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2026-11-24] - 2026-11-24

### Fixed
- **2026 OpenTheRank SSR table parsing (`openrank/pricing.py`)**:
  - Exclude non-tier `Tools` (`Get VPN` / `Get Address`) and `Data snapshot` columns (plus `.ctl-snap-td` / `.ctl-td-address` / `.ctl-has-vpn` cells) from `additional_tiers`.
  - Extract ISO `snapshot_date` (`YYYY-MM-DD`) from `.ctl-snap[data-date]` / `data-at` or `Data snapshot` cell text when `tr[data-snapshot]` is absent.
  - Extract clean `.ctl-pill` USD prices and `.ctl-local` local currency strings on 2026 OpenTheRank cells, and strip inline `<script>` / `<style>` blocks inside `HTMLNode.text()`.
  - Bind multiple per-tier `VS US Price` columns (`data-vs-x-cell`, header tier name, `data-vs-usd`) to their exact `PriceTier`, derive missing per-tier deltas from same-platform US tier prices, and keep `vs_us_delta` strictly aligned with `primary_tier.delta_percent`.
  - Fix `_determine_table_platform()` so Google Play tables (`data-vs-prices="play:..."`) are never misclassified as `appstore` by an ancestor `<main>` heading, read headers from `<thead>` only, and skip cross-platform summary comparison tables (`table.gpvs__table`).
- **DNS proxy fallback (`openrank/client.py`)**: On local DNS resolution failure (`socket.gaierror` / `nodename nor servname`), transparently fall back to explicit proxy env vars or active local loopback proxy (`127.0.0.1:7897` / `7890`) while skipping loopback and RFC-2606 reserved test domains.
- **Global PATH links**: Exposed `openrank` and `otr` via `~/.local/bin/{openrank,otr}`.

## [2026-11-13] - 2026-11-13

### Maintenance
- Auto-sanitize privacy files and local paths (`9765efd`)

## [2026-10-19] - 2026-10-19

### Maintenance
- Enforce GitHub Actions quota hygiene (ubuntu-latest default, concurrency, paths-ignore) (`332c8ae`)

## [2026-10-13] - 2026-10-13

### Fixes
- Ensure safe error close on uninitialized fp in Python 3.9 (`febec10`)

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
