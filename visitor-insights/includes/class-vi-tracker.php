<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Public-facing pieces: enqueues the front-end tracking beacon and exposes
 * the anonymous REST endpoint it posts to. Also owns the daily retention
 * cleanup cron handler, since that operates on the same tables.
 */
class VI_Tracker {

	const NAMESPACE_ = 'visitor-insights/v1';

	public function init() {
		add_action( 'wp_enqueue_scripts', array( $this, 'enqueue_tracker_script' ) );
		add_action( 'rest_api_init', array( $this, 'register_routes' ) );
		add_action( 'vi_daily_retention_cleanup', array( $this, 'run_retention_cleanup' ) );
	}

	public function enqueue_tracker_script() {
		// Don't track the site owner's own logged-in admin sessions unless
		// they've explicitly opted in - avoids inflating stats with your
		// own testing/editing traffic.
		if ( is_admin() ) {
			return;
		}
		if ( current_user_can( 'manage_options' ) && ! get_option( 'vi_track_logged_in_admins', false ) ) {
			return;
		}

		wp_enqueue_script(
			'vi-tracker',
			VI_PLUGIN_URL . 'assets/js/tracker.js',
			array(),
			VI_VERSION,
			true
		);

		wp_localize_script(
			'vi-tracker',
			'VI_TRACKER',
			array(
				'restUrl' => esc_url_raw( rest_url( self::NAMESPACE_ . '/track' ) ),
			)
		);
	}

	public function register_routes() {
		register_rest_route(
			self::NAMESPACE_,
			'/track',
			array(
				'methods'             => 'POST',
				'callback'            => array( $this, 'handle_track' ),
				'permission_callback' => '__return_true', // Intentionally public: anonymous visitor beacon.
				'args'                => array(
					'session_id' => array( 'required' => true ),
					'path'       => array( 'required' => true ),
				),
			)
		);
	}

	public function handle_track( WP_REST_Request $request ) {
		$session_id = $this->sanitize_session_id( $request->get_param( 'session_id' ) );
		if ( ! $session_id ) {
			return new WP_REST_Response( array( 'ok' => false ), 400 );
		}

		$ip = $this->get_client_ip();

		if ( ! $this->rate_limit_ok( $ip ) ) {
			return new WP_REST_Response( array( 'ok' => false, 'reason' => 'rate_limited' ), 429 );
		}

		$path = $this->truncate( sanitize_text_field( wp_unslash( (string) $request->get_param( 'path' ) ) ), 512 );
		if ( $this->is_excluded_path( $path ) ) {
			return new WP_REST_Response( array( 'ok' => true, 'tracked' => false ) );
		}

		// Truncate every variable-length field to its column's max length -
		// visitor-controlled values (referrer, UTM params especially) can
		// run far longer than a real title/campaign name would, and MySQL
		// strict mode rejects the whole insert rather than truncating it.
		$title         = $this->truncate( sanitize_text_field( wp_unslash( (string) $request->get_param( 'title' ) ) ), 255 );
		$referrer      = $this->truncate( esc_url_raw( (string) $request->get_param( 'referrer' ) ), 512 );
		$language      = $this->truncate( sanitize_text_field( (string) $request->get_param( 'language' ) ), 20 );
		$screen        = (array) $request->get_param( 'screen' );
		$screen_width  = isset( $screen['width'] ) ? absint( $screen['width'] ) : null;
		$screen_height = isset( $screen['height'] ) ? absint( $screen['height'] ) : null;

		$utm_raw = (array) $request->get_param( 'utm' );
		$utm     = array(
			'source'   => $this->truncate( sanitize_text_field( $utm_raw['source'] ?? '' ), 255 ),
			'medium'   => $this->truncate( sanitize_text_field( $utm_raw['medium'] ?? '' ), 255 ),
			'campaign' => $this->truncate( sanitize_text_field( $utm_raw['campaign'] ?? '' ), 255 ),
			'term'     => $this->truncate( sanitize_text_field( $utm_raw['term'] ?? '' ), 255 ),
			'content'  => $this->truncate( sanitize_text_field( $utm_raw['content'] ?? '' ), 255 ),
		);

		$user_agent = $this->truncate( sanitize_text_field( wp_unslash( $_SERVER['HTTP_USER_AGENT'] ?? '' ) ), 512 );

		global $wpdb;
		$sessions_table  = $wpdb->prefix . VI_TABLE_SESSIONS;
		$pageviews_table = $wpdb->prefix . VI_TABLE_PAGEVIEWS;
		$now             = current_time( 'mysql', true );

		$existing = $wpdb->get_row(
			$wpdb->prepare( "SELECT id FROM {$sessions_table} WHERE session_id = %s", $session_id )
		);

		if ( $existing ) {
			$wpdb->query(
				$wpdb->prepare(
					"UPDATE {$sessions_table} SET last_seen = %s, page_count = page_count + 1 WHERE session_id = %s",
					$now,
					$session_id
				)
			);
		} else {
			$geo = VI_Geo::lookup( $ip );

			$wpdb->insert(
				$sessions_table,
				array(
					'session_id'    => $session_id,
					'ip'            => $ip,
					'country'       => $geo['country'],
					'region'        => $geo['region'],
					'city'          => $geo['city'],
					'zip'           => $geo['zip'],
					'lat'           => $geo['lat'],
					'lng'           => $geo['lng'],
					'isp'           => $geo['isp'],
					'org'           => $geo['org'],
					'is_mobile'     => $geo['is_mobile'] ? 1 : 0,
					'is_proxy'      => $geo['is_proxy'] ? 1 : 0,
					'is_hosting'    => $geo['is_hosting'] ? 1 : 0,
					'user_agent'    => $user_agent,
					'language'      => $language,
					'screen_width'  => $screen_width,
					'screen_height' => $screen_height,
					'referrer'      => $referrer,
					'landing_page'  => $path,
					'utm_source'    => $utm['source'],
					'utm_medium'    => $utm['medium'],
					'utm_campaign'  => $utm['campaign'],
					'utm_term'      => $utm['term'],
					'utm_content'   => $utm['content'],
					'first_seen'    => $now,
					'last_seen'     => $now,
					'page_count'    => 1,
				)
			);
		}

		$wpdb->insert(
			$pageviews_table,
			array(
				'session_id' => $session_id,
				'path'       => $path,
				'title'      => $title,
				'viewed_at'  => $now,
			)
		);

		return new WP_REST_Response( array( 'ok' => true ) );
	}

	public function run_retention_cleanup() {
		$days = absint( get_option( 'vi_retention_days', 365 ) );
		if ( $days <= 0 ) {
			return; // 0 == keep forever.
		}

		global $wpdb;
		$sessions_table  = $wpdb->prefix . VI_TABLE_SESSIONS;
		$pageviews_table = $wpdb->prefix . VI_TABLE_PAGEVIEWS;
		$cutoff          = gmdate( 'Y-m-d H:i:s', time() - ( $days * DAY_IN_SECONDS ) );

		$stale_ids = $wpdb->get_col(
			$wpdb->prepare( "SELECT session_id FROM {$sessions_table} WHERE last_seen < %s", $cutoff )
		);

		if ( empty( $stale_ids ) ) {
			return;
		}

		$placeholders = implode( ',', array_fill( 0, count( $stale_ids ), '%s' ) );
		// phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared -- placeholders built from a fixed-length array of %s above, values passed positionally below.
		$wpdb->query( $wpdb->prepare( "DELETE FROM {$pageviews_table} WHERE session_id IN ({$placeholders})", $stale_ids ) );
		$wpdb->query( $wpdb->prepare( "DELETE FROM {$sessions_table} WHERE session_id IN ({$placeholders})", $stale_ids ) );
	}

	private function sanitize_session_id( $session_id ) {
		$session_id = (string) $session_id;
		if ( ! preg_match( '/^[a-zA-Z0-9\-]{8,64}$/', $session_id ) ) {
			return '';
		}
		return $session_id;
	}

	private function truncate( $value, $max_length ) {
		return mb_substr( (string) $value, 0, $max_length );
	}

	private function is_excluded_path( $path ) {
		$excluded = get_option( 'vi_excluded_paths', '' );
		if ( empty( $excluded ) ) {
			return false;
		}
		foreach ( array_filter( array_map( 'trim', explode( ',', $excluded ) ) ) as $prefix ) {
			if ( 0 === strpos( $path, $prefix ) ) {
				return true;
			}
		}
		return false;
	}

	/**
	 * Best-effort client IP. X-Forwarded-For is attacker-controllable
	 * unless the site is genuinely behind a trusted proxy/CDN, but this is
	 * the standard tradeoff every self-contained WP analytics plugin makes.
	 */
	private function get_client_ip() {
		$candidates = array( 'HTTP_CF_CONNECTING_IP', 'HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR' );
		foreach ( $candidates as $key ) {
			if ( empty( $_SERVER[ $key ] ) ) {
				continue;
			}
			$value = sanitize_text_field( wp_unslash( $_SERVER[ $key ] ) );
			$first = trim( explode( ',', $value )[0] );
			if ( filter_var( $first, FILTER_VALIDATE_IP ) ) {
				return $first;
			}
		}
		return '';
	}

	private function rate_limit_ok( $ip ) {
		if ( empty( $ip ) ) {
			return true; // Can't key a limit without an IP; let geo lookup skip handle the rest.
		}
		$key   = 'vi_rl_' . md5( $ip );
		$count = (int) get_transient( $key );
		if ( $count >= 60 ) {
			return false;
		}
		set_transient( $key, $count + 1, MINUTE_IN_SECONDS );
		return true;
	}
}
