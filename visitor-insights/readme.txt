=== Visitor Insights & Skip Trace ===
Contributors: yourwpusername
Tags: analytics, visitor tracking, traffic stats, skip trace, reporting
Requires at least: 5.8
Tested up to: 6.7
Requires PHP: 7.4
Stable tag: 1.1.1
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Self-contained visitor & traffic analytics for WordPress, with CSV/PDF reporting and an optional identity skip-trace lookup.

== Description ==

Visitor Insights adds a **Visitors** tab to your WordPress admin dashboard showing who's on your site and where they came from - no external analytics account required, no data leaves your own database.

**Core features**

* Automatic session + pageview tracking via a lightweight front-end script (no cookies banner required - uses a local-storage session id, not third-party cookies)
* Country / region / city / ISP lookup for every new session (free ip-api.com service, no API key needed)
* Referrer and UTM campaign tracking (source, medium, campaign, term, content)
* Device signal flags: mobile, proxy/VPN, hosting/datacenter IP
* Searchable, date-filterable sessions table in wp-admin, with a traffic-source filter (Google Ads, Facebook/Instagram Ads, organic search, organic social, direct)
* One-click CSV export of the current filtered view
* PDF export built in - no extra setup, no dependencies to install
* Configurable data retention with automatic daily cleanup
* Excludes your own logged-in admin visits by default

**Optional: Skip Trace**

Given a name, address, phone, or email you already have for a visitor (e.g. from a form submission), Skip Trace can look up additional contact details via a third-party data provider (Apify). This is off by default and requires two separate opt-ins in Settings, because identity lookups carry real privacy/legal obligations that vary by jurisdiction - review your own legal requirements (GDPR, CCPA, or applicable local law) before enabling this feature.

== Installation ==

1. Upload the `visitor-insights` folder to `/wp-content/plugins/`, or install the zip via Plugins → Add New → Upload Plugin.
2. Activate the plugin through the 'Plugins' menu in WordPress.
3. A new **Visitors** menu item appears in wp-admin with your traffic stats. CSV and PDF export both work immediately - no extra setup, no dependencies.
4. (Optional, for Skip Trace) Go to Visitors → Settings, add your Apify API token, and check both the "Enable skip trace" and consent acknowledgement boxes.

== Frequently Asked Questions ==

= Does this send my visitors' data to any third party? =

Core tracking (sessions, pageviews, referrers) is stored entirely in your own WordPress database. Two features do call external services: IP geolocation (ip-api.com, can be disabled in Settings) and the optional Skip Trace lookup (Apify, off by default).

= Does this need a cookie consent banner? =

The tracker uses `localStorage`, not cookies, to remember a visitor's session id. Whether this requires disclosure under your applicable privacy law is a legal question for your own site/jurisdiction - this plugin does not provide legal advice.

= What happens to my data if I uninstall the plugin? =

Deactivating keeps all data intact. Choosing "Delete" from the Plugins screen permanently drops the plugin's database tables and settings.

== Changelog ==

= 1.1.1 =
* Fix: the Source filter (Google Ads / Facebook Ads / etc.) broke the Sessions list with a JSON error - the SQL LIKE wildcards it used weren't escaped for $wpdb->prepare(), which treats every "%" as a placeholder.

= 1.1.0 =
* Add traffic-source filter and column: Google Ads, Facebook/Instagram Ads, organic search, organic social, direct.
* Fix: Skip Trace modal Close button not working (CSS specificity issue with the `hidden` attribute).
* Switch PDF export to a bundled dependency-free writer (previously required an optional Composer library).

= 1.0.0 =
* Initial release: session/pageview tracking, geolocation, CSV/PDF export, optional Apify skip trace.

== Upgrade Notice ==

= 1.1.0 =
Traffic-source filtering, modal-close fix, dependency-free PDF export.
