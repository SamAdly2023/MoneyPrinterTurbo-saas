<?php
/**
 * Plugin Name:       Visitor Insights & Skip Trace
 * Plugin URI:        https://example.com/visitor-insights
 * Description:       Self-contained visitor & traffic analytics for WordPress - sessions, pageviews, referrers, geo/device breakdown, CSV/PDF reports, and optional Apify-powered skip trace lookups.
 * Version:           1.1.1
 * Requires at least: 5.8
 * Requires PHP:      7.4
 * Author:            Your Company
 * Author URI:        https://example.com
 * License:           GPL v2 or later
 * License URI:       https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain:       visitor-insights
 * Domain Path:       /languages
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit; // No direct access.
}

define( 'VI_VERSION', '1.1.1' );
define( 'VI_PLUGIN_FILE', __FILE__ );
define( 'VI_PLUGIN_DIR', plugin_dir_path( __FILE__ ) );
define( 'VI_PLUGIN_URL', plugin_dir_url( __FILE__ ) );
define( 'VI_TEXT_DOMAIN', 'visitor-insights' );

// Custom DB table base names (actual table gets $wpdb->prefix applied at use time).
define( 'VI_TABLE_SESSIONS', 'vi_sessions' );
define( 'VI_TABLE_PAGEVIEWS', 'vi_pageviews' );
define( 'VI_TABLE_ENRICHMENT', 'vi_enrichment' );

require_once VI_PLUGIN_DIR . 'includes/class-vi-activator.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-deactivator.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-geo.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-source.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-tracker.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-skiptrace.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-pdf-writer.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-export.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-rest-controller.php';
require_once VI_PLUGIN_DIR . 'includes/class-vi-admin.php';

register_activation_hook( VI_PLUGIN_FILE, array( 'VI_Activator', 'activate' ) );
register_deactivation_hook( VI_PLUGIN_FILE, array( 'VI_Deactivator', 'deactivate' ) );

/**
 * Bumps the DB schema forward on plugin upgrade (e.g. auto-update), not just
 * on a fresh activation - dbDelta() is idempotent so this is safe to call
 * unconditionally whenever the stored schema version is behind VI_VERSION.
 */
function vi_maybe_upgrade() {
	$installed_version = get_option( 'vi_db_version', '' );
	if ( $installed_version !== VI_VERSION ) {
		VI_Activator::create_tables();
		update_option( 'vi_db_version', VI_VERSION );
	}
}
add_action( 'plugins_loaded', 'vi_maybe_upgrade' );

/**
 * Load translations.
 */
function vi_load_textdomain() {
	load_plugin_textdomain( VI_TEXT_DOMAIN, false, dirname( plugin_basename( VI_PLUGIN_FILE ) ) . '/languages' );
}
add_action( 'plugins_loaded', 'vi_load_textdomain' );

/**
 * Wire up the pieces. Tracker + REST routes are needed on every request
 * (front-end pageviews and the REST API both fire outside /wp-admin), the
 * admin menu only needs to exist inside wp-admin.
 */
function vi_bootstrap() {
	$tracker = new VI_Tracker();
	$tracker->init();

	$rest_controller = new VI_REST_Controller();
	$rest_controller->init();

	if ( is_admin() ) {
		$admin = new VI_Admin();
		$admin->init();
	}
}
add_action( 'init', 'vi_bootstrap' );
