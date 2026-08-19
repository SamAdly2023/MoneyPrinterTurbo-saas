<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Runs on plugin activation (and on version bumps via vi_maybe_upgrade()).
 * Creates the plugin's own tables so it never touches core WP tables.
 */
class VI_Activator {

	public static function activate() {
		self::create_tables();
		self::set_default_options();
		update_option( 'vi_db_version', VI_VERSION );

		if ( ! wp_next_scheduled( 'vi_daily_retention_cleanup' ) ) {
			wp_schedule_event( time(), 'daily', 'vi_daily_retention_cleanup' );
		}
	}

	public static function create_tables() {
		global $wpdb;

		require_once ABSPATH . 'wp-admin/includes/upgrade.php';

		$charset_collate = $wpdb->get_charset_collate();

		$sessions_table   = $wpdb->prefix . VI_TABLE_SESSIONS;
		$pageviews_table  = $wpdb->prefix . VI_TABLE_PAGEVIEWS;
		$enrichment_table = $wpdb->prefix . VI_TABLE_ENRICHMENT;

		$sql_sessions = "CREATE TABLE {$sessions_table} (
			id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
			session_id VARCHAR(64) NOT NULL,
			ip VARCHAR(45) NOT NULL DEFAULT '',
			country VARCHAR(100) NOT NULL DEFAULT '',
			region VARCHAR(100) NOT NULL DEFAULT '',
			city VARCHAR(100) NOT NULL DEFAULT '',
			zip VARCHAR(20) NOT NULL DEFAULT '',
			lat DECIMAL(10,6) NULL,
			lng DECIMAL(10,6) NULL,
			isp VARCHAR(255) NOT NULL DEFAULT '',
			org VARCHAR(255) NOT NULL DEFAULT '',
			is_mobile TINYINT(1) NOT NULL DEFAULT 0,
			is_proxy TINYINT(1) NOT NULL DEFAULT 0,
			is_hosting TINYINT(1) NOT NULL DEFAULT 0,
			user_agent VARCHAR(512) NOT NULL DEFAULT '',
			language VARCHAR(20) NOT NULL DEFAULT '',
			screen_width SMALLINT UNSIGNED NULL,
			screen_height SMALLINT UNSIGNED NULL,
			referrer VARCHAR(512) NOT NULL DEFAULT '',
			landing_page VARCHAR(512) NOT NULL DEFAULT '',
			utm_source VARCHAR(255) NOT NULL DEFAULT '',
			utm_medium VARCHAR(255) NOT NULL DEFAULT '',
			utm_campaign VARCHAR(255) NOT NULL DEFAULT '',
			utm_term VARCHAR(255) NOT NULL DEFAULT '',
			utm_content VARCHAR(255) NOT NULL DEFAULT '',
			first_seen DATETIME NOT NULL,
			last_seen DATETIME NOT NULL,
			page_count INT UNSIGNED NOT NULL DEFAULT 0,
			identified TINYINT(1) NOT NULL DEFAULT 0,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			PRIMARY KEY  (id),
			UNIQUE KEY session_id (session_id),
			KEY last_seen (last_seen),
			KEY ip (ip)
		) {$charset_collate};";

		$sql_pageviews = "CREATE TABLE {$pageviews_table} (
			id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
			session_id VARCHAR(64) NOT NULL,
			path VARCHAR(512) NOT NULL DEFAULT '',
			title VARCHAR(255) NOT NULL DEFAULT '',
			viewed_at DATETIME NOT NULL,
			PRIMARY KEY  (id),
			KEY session_id (session_id),
			KEY viewed_at (viewed_at)
		) {$charset_collate};";

		$sql_enrichment = "CREATE TABLE {$enrichment_table} (
			id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
			session_id VARCHAR(64) NOT NULL,
			seed_data LONGTEXT NOT NULL,
			results LONGTEXT NOT NULL,
			enriched_at DATETIME NOT NULL,
			PRIMARY KEY  (id),
			KEY session_id (session_id)
		) {$charset_collate};";

		dbDelta( $sql_sessions );
		dbDelta( $sql_pageviews );
		dbDelta( $sql_enrichment );
	}

	private static function set_default_options() {
		add_option( 'vi_retention_days', 365 );
		add_option( 'vi_track_logged_in_admins', false );
		add_option( 'vi_geo_lookup_enabled', true );
		add_option( 'vi_skip_trace_enabled', false );
		add_option( 'vi_skip_trace_consent_ack', false );
		add_option( 'vi_apify_token', '' );
		add_option( 'vi_excluded_paths', '' );
	}
}
