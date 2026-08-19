<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Runs on plugin deactivation. Tables/options are intentionally left in
 * place here - only uninstall.php (explicit "Delete" from the Plugins
 * screen) removes data, so deactivating for a quick test doesn't lose
 * anything.
 */
class VI_Deactivator {

	public static function deactivate() {
		$timestamp = wp_next_scheduled( 'vi_daily_retention_cleanup' );
		if ( $timestamp ) {
			wp_unschedule_event( $timestamp, 'vi_daily_retention_cleanup' );
		}
	}
}
