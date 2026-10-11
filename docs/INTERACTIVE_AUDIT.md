# OpenRank Dashboard Interactive Audit & Facade Inventory

> Objective: Complete inventory of all interactive UI surfaces to guarantee the **Zero-Facade Invariant** (Rule 0).
> Every button, input, tab, modal, and drawer is bridged to real backend API endpoints, persistent storage, and verified state transitions. ZERO mock delays, ZERO dead clicks, ZERO placeholder toasts.

---

## 1. Interactive Surface Inventory

| Area | Element & Action | Current State | Target Production Behavior | Persistence & API Destination |
| :--- | :--- | :--- | :--- | :--- |
| **Header** | Brand Logo / Title Click | Initialized | Navigates to default overview / resets active query filters | Client state & Route hash (`#overview`) |
| **Header** | Global Search Input (`input#global-search`) | Initialized | Debounced (150ms) dynamic query to catalog; triggers dropdown suggestions with real scores | `GET /api/catalog/search?q=<query>` |
| **Header** | Autocomplete Suggestion Item Click | Initialized | Resolves exact product slug, updates URL hash, loads real multi-region pricing table | `GET /api/pricing?product=<slug>` & `POST /api/history` |
| **Header** | Connection / Health Indicator Badge | Initialized | Displays live server status, cache freshness, and latency | `GET /api/health` polling / on-load |
| **Header** | Settings Button (`button#btn-settings`) | Initialized | Opens persistent Settings modal with current configured values | Client Modal DOM (`#modal-settings`) |
| **Header** | History / Recents Button (`button#btn-history`) | Initialized | Opens History Drawer showing real past searches and pricing queries | `GET /api/history` |
| **Sidebar / Tabs** | Category Nav Pills (`All`, `AI`, `Streaming`, `Gaming`, `Hardware`) | Initialized | Filters product catalog grid by category in real-time | Client Filter State & `GET /api/catalog/search?category=<cat>` |
| **Sidebar / Tabs** | Subsystem Views (`Arbitrage Radar`, `Apple Validator`, `System Diagnostics`) | Initialized | Switches active view mode between Pricing Matrix, Apple Validator, and Diagnostics | Client View State Router |
| **Main - Pricing** | Product Card / Row Click | Initialized | Loads selected product's real-time pricing matrix across all storefronts | `GET /api/pricing?product=<slug>` & `POST /api/history` |
| **Main - Pricing** | Storefront Platform Pills (`All`, `App Store`, `Google Play`, `Web`) | Initialized | Re-filters active pricing table strictly by platform | `GET /api/pricing?product=<slug>&platform=<plat>` |
| **Main - Pricing** | Country ISO2 Filter Input (`input#filter-country`) | Initialized | Filters pricing records by 2-letter ISO country code without false substring matches | Client-side reactive filter on backend dataset |
| **Main - Pricing** | Sort Selector (`Cheapest USD`, `Max Discount %`, `Alphabetical`) | Initialized | Dynamically re-sorts table rows based on computed currency metrics | Client-side reactive sort |
| **Main - Pricing** | Refresh Button (`button#btn-refresh-pricing`) | Initialized | Bypasses local 24h cache and forces fresh remote data retrieval | `GET /api/pricing?product=<slug>&refresh=true` |
| **Main - Pricing** | Export CSV Button (`button#btn-export-csv`) | Initialized | Generates and downloads real CSV file containing all visible pricing records | Genuine Browser Blob Download (`text/csv`) |
| **Main - Pricing** | Export JSON Button (`button#btn-export-json`) | Initialized | Generates and downloads structured JSON payload matching schema | Genuine Browser Blob Download (`application/json`) |
| **Main - Pricing** | Watchlist / Bookmark Toggle (`button#btn-bookmark`) | Initialized | Adds/removes current product from user's watchlist; updates badge | `POST /api/watchlist` & `localStorage` mirror |
| **Apple Validator** | Validate Storefront Button (`button#btn-validate-apple`) | Initialized | Queries Apple App Store CDN SSR hydration for real IAP tier pricing | `GET /api/validate/apple?product=<slug>&country=<cc>` |
| **Apple Validator** | Cross-Validate Checkbox (`input#chk-cross-validate`) | Initialized | Compares OpenTheRank pricing table against Apple official IAPs and displays parity badges | `openrank.storefront_apple.cross_validate_pricing()` |
| **Settings Modal** | Preferred Base Currency Select (`USD`, `EUR`, `GBP`, `JPY`, `PHP`) | Initialized | Updates base comparison currency for delta math and displays | `POST /api/settings` & `~/.config/openrank/settings.json` |
| **Settings Modal** | Default Platform Select (`All`, `App Store`, `Google Play`, `Web`) | Initialized | Configures default storefront filter across sessions | `POST /api/settings` & `~/.config/openrank/settings.json` |
| **Settings Modal** | Cache TTL Slider / Input (hours) | Initialized | Adjusts cache TTL and persists to backend config | `POST /api/settings` & `~/.config/openrank/settings.json` |
| **Settings Modal** | Save Settings Button (`button#btn-save-settings`) | Initialized | Saves settings to backend disk store, shows feedback, and refreshes UI | `POST /api/settings` (Survives hard page refresh) |
| **History Drawer** | History Item Click | Initialized | Loads the clicked historical query immediately into the main view | `GET /api/pricing?product=<slug>` |
| **History Drawer** | Clear History Button (`button#btn-clear-history`) | Initialized | Purges search history from server store and resets drawer | `DELETE /api/history` |
| **Diagnostics** | Run Diagnostics Button (`button#btn-run-diag`) | Initialized | Executes live health probes across cache, remote endpoints, and Apple CDN | `GET /api/diagnose` |

---

## 2. Zero-Facade Invariant Verifications

1. **No Simulated Timers**: No `setTimeout` or `setInterval` simulating network requests. All loading spinners bind strictly to `fetch()` Promise lifecycles.
2. **No Static Demo Arrays**: Catalog items and pricing rows are populated 100% from `CatalogManager` and `PricingEngine` backend data.
3. **No Dead Clicks**: Every interactive element is backed by an event listener with observable side-effects.
4. **Persistence Guarantee**: Settings, bookmarks, and search history persist to disk (`openrank/data/` or user config directory) and survive browser restarts and hard page refreshes (`Cmd+Shift+R` / `F5`).
5. **Console Cleanliness**: Zero uncaught JavaScript exceptions, zero unhandled Promise rejections, and zero 404 resource errors.
