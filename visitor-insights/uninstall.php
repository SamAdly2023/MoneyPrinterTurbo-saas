<?php
/**
 * Fires only when the plugin is explicitly deleted from the Plugins screen
 * (not on a plain deactivate) - WordPress requires this exact filename and
 * loads it directly, so ABSPATH won't be defined; WP_UNINSTALL_PLUGIN is
 * the correct guard here instead.
 */

if ( ! defined( 'WP_UNINSTALL_PLUGIN' ) ) {
	exit;
}

global $wpdb;

$tables = array(
	$wpdb->prefix . 'vi_sessions',
	$wpdb->prefix . 'vi_pageviews',
	$wpdb->prefix . 'vi_enrichment',
);

foreach ( $tables as $table ) {
	$wpdb->query( "DROP TABLE IF EXISTS {$table}" ); // phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared -- table name is a fixed prefix + hardcoded suffix, no user input.
}

$options = array(
	'vi_db_version',
	'vi_retention_days',
	'vi_track_logged_in_admins',
	'vi_geo_lookup_enabled',
	'vi_skip_trace_enabled',
	'vi_skip_trace_consent_ack',
	'vi_apify_token',
	'vi_excluded_paths',
);

foreach ( $options as $option ) {
	delete_option( $option );
}

$timestamp = wp_next_scheduled( 'vi_daily_retention_cleanup' );
if ( $timestamp ) {
	wp_unschedule_event( $timestamp, 'vi_daily_retention_cleanup' );
}
