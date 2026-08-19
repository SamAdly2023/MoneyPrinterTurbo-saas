<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Admin-only REST routes backing the Visitors dashboard page: listing
 * sessions, a stats summary, triggering skip trace, and CSV/PDF export.
 * Cookie-authenticated requests are already required to carry a valid
 * X-WP-Nonce by WordPress core; can_manage() below adds the actual
 * authorization check on top of that built-in CSRF check.
 */
class VI_REST_Controller {

	const NAMESPACE_ = 'visitor-insights/v1';

	public function init() {
		add_action( 'rest_api_init', array( $this, 'register_routes' ) );
	}

	public function register_routes() {
		register_rest_route(
			self::NAMESPACE_,
			'/sessions',
			array(
				'methods'             => 'GET',
				'callback'            => array( $this, 'get_sessions' ),
				'permission_callback' => array( $this, 'can_manage' ),
			)
		);

		register_rest_route(
			self::NAMESPACE_,
			'/stats',
			array(
				'methods'             => 'GET',
				'callback'            => array( $this, 'get_stats' ),
				'permission_callback' => array( $this, 'can_manage' ),
			)
		);

		register_rest_route(
			self::NAMESPACE_,
			'/skip-trace',
			array(
				'methods'             => 'POST',
				'callback'            => array( $this, 'post_skip_trace' ),
				'permission_callback' => array( $this, 'can_manage' ),
			)
		);

	}

	public function can_manage() {
		return current_user_can( 'manage_options' );
	}

	/**
	 * Shared WHERE-clause builder for the sessions list, stats, and export
	 * endpoints so filtering stays consistent across all three. Takes a
	 * plain params array (works for both $request->get_params() from a
	 * REST call and $_GET from the admin-post export handler).
	 */
	public static function build_filters( array $params ) {
		global $wpdb;

		$where  = array( '1=1' );
		$values = array();

		$search = sanitize_text_field( (string) ( $params['search'] ?? '' ) );
		if ( $search ) {
			$like     = '%' . $wpdb->esc_like( $search ) . '%';
			$where[]  = '(ip LIKE %s OR country LIKE %s OR city LIKE %s OR referrer LIKE %s OR landing_page LIKE %s)';
			$values[] = $like;
			$values[] = $like;
			$values[] = $like;
			$values[] = $like;
			$values[] = $like;
		}

		$date_from = sanitize_text_field( (string) ( $params['date_from'] ?? '' ) );
		if ( $date_from ) {
			$where[]  = 'last_seen >= %s';
			$values[] = $date_from . ' 00:00:00';
		}

		$date_to = sanitize_text_field( (string) ( $params['date_to'] ?? '' ) );
		if ( $date_to ) {
			$where[]  = 'last_seen <= %s';
			$values[] = $date_to . ' 23:59:59';
		}

		$source = sanitize_key( (string) ( $params['source'] ?? '' ) );
		$source_condition = VI_Source::sql_condition( $source );
		if ( $source_condition ) {
			$where[] = $source_condition;
		}

		return array( implode( ' AND ', $where ), $values );
	}

	public function get_sessions( WP_REST_Request $request ) {
		global $wpdb;
		$sessions_table = $wpdb->prefix . VI_TABLE_SESSIONS;

		$page     = max( 1, absint( $request->get_param( 'page' ) ?: 1 ) );
		$per_page = min( 200, max( 1, absint( $request->get_param( 'per_page' ) ?: 50 ) ) );
		$offset   = ( $page - 1 ) * $per_page;

		list( $where, $values ) = self::build_filters( $request->get_params() );

		$count_sql = "SELECT COUNT(*) FROM {$sessions_table} WHERE {$where}";
		$total     = (int) $wpdb->get_var( $wpdb->prepare( $count_sql, $values ) ); // phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared -- always run through prepare() even with an empty $values so any literal %% in $where (from VI_Source) gets correctly reduced to %.

		$list_sql      = "SELECT * FROM {$sessions_table} WHERE {$where} ORDER BY last_seen DESC LIMIT %d OFFSET %d";
		$list_values   = array_merge( $values, array( $per_page, $offset ) );
		$rows          = $wpdb->get_results( $wpdb->prepare( $list_sql, $list_values ), ARRAY_A );

		foreach ( $rows as &$row ) {
			$row['source_label'] = VI_Source::label( $row );
		}
		unset( $row );

		return new WP_REST_Response(
			array(
				'sessions' => $rows,
				'total'    => $total,
				'page'     => $page,
				'per_page' => $per_page,
			)
		);
	}

	public function get_stats( WP_REST_Request $request ) {
		global $wpdb;
		$sessions_table = $wpdb->prefix . VI_TABLE_SESSIONS;

		list( $where, $values ) = self::build_filters( $request->get_params() );

		$base = "FROM {$sessions_table} WHERE {$where}";

		$sessions   = (int) self::scalar( $wpdb, "SELECT COUNT(*) {$base}", $values );
		$pageviews  = (int) self::scalar( $wpdb, "SELECT COALESCE(SUM(page_count),0) {$base}", $values );
		$countries  = (int) self::scalar( $wpdb, "SELECT COUNT(DISTINCT country) {$base} AND country != ''", $values );
		$identified = (int) self::scalar( $wpdb, "SELECT COUNT(*) {$base} AND identified = 1", $values );

		return new WP_REST_Response(
			array(
				'sessions'   => $sessions,
				'pageviews'  => $pageviews,
				'countries'  => $countries,
				'identified' => $identified,
			)
		);
	}

	public static function scalar( $wpdb, $sql, $values ) {
		// Always run through prepare() - even with an empty $values array -
		// so any literal %% in $sql (from VI_Source's LIKE patterns) is
		// correctly reduced to a literal % rather than left doubled.
		return $wpdb->get_var( $wpdb->prepare( $sql, $values ) ); // phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared
	}

	public function post_skip_trace( WP_REST_Request $request ) {
		$body = $request->get_json_params();
		if ( ! is_array( $body ) ) {
			$body = array();
		}

		$seed = array(
			'name'    => $body['name'] ?? '',
			'address' => $body['address'] ?? '',
			'city'    => $body['city'] ?? '',
			'state'   => $body['state'] ?? '',
			'zip'     => $body['zip'] ?? '',
			'phone'   => $body['phone'] ?? '',
			'email'   => $body['email'] ?? '',
		);

		$results = VI_Skiptrace::run( $seed );

		if ( is_wp_error( $results ) ) {
			return new WP_REST_Response(
				array( 'ok' => false, 'error' => $results->get_error_message() ),
				400
			);
		}

		$session_id = sanitize_text_field( (string) ( $body['session_id'] ?? '' ) );
		if ( $session_id ) {
			global $wpdb;
			$sessions_table   = $wpdb->prefix . VI_TABLE_SESSIONS;
			$enrichment_table = $wpdb->prefix . VI_TABLE_ENRICHMENT;
			$now              = current_time( 'mysql', true );

			$wpdb->insert(
				$enrichment_table,
				array(
					'session_id'  => $session_id,
					'seed_data'   => wp_json_encode( $seed ),
					'results'     => wp_json_encode( $results ),
					'enriched_at' => $now,
				)
			);

			$wpdb->update(
				$sessions_table,
				array( 'identified' => 1 ),
				array( 'session_id' => $session_id )
			);
		}

		return new WP_REST_Response( array( 'ok' => true, 'results' => $results ) );
	}
}
