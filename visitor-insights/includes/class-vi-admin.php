<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * The wp-admin side: the "Visitors" menu page (stats/table/skip-trace/
 * export, all driven by REST calls from admin.js) plus a Settings section
 * registered through the standard WP Settings API.
 */
class VI_Admin {

	const PAGE_SLUG = 'visitor-insights';

	public function init() {
		add_action( 'admin_menu', array( $this, 'register_menu' ) );
		add_action( 'admin_enqueue_scripts', array( $this, 'enqueue_assets' ) );
		add_action( 'admin_init', array( $this, 'register_settings' ) );
		add_action( 'admin_post_vi_export', array( $this, 'handle_export' ) );
	}

	/**
	 * CSV/PDF export, triggered by a plain browser navigation from
	 * admin.js (not fetch/XHR) - admin-post.php is the standard WP pattern
	 * for wp-admin file downloads, avoiding any friction with the REST
	 * server's own response/header handling.
	 */
	public function handle_export() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You do not have permission to do this.', 'visitor-insights' ), '', array( 'response' => 403 ) );
		}
		check_admin_referer( 'vi_export' );

		$format = ( 'pdf' === ( $_GET['format'] ?? '' ) ) ? 'pdf' : 'csv';

		list( $where, $values ) = VI_REST_Controller::build_filters( wp_unslash( $_GET ) );

		global $wpdb;
		$sessions_table = $wpdb->prefix . VI_TABLE_SESSIONS;
		$sql            = "SELECT * FROM {$sessions_table} WHERE {$where} ORDER BY last_seen DESC LIMIT 5000";
		// Always run through prepare() - even with an empty $values array -
		// so any literal %% in $where (from VI_Source) is correctly
		// reduced to a literal % rather than left doubled.
		$rows           = $wpdb->get_results( $wpdb->prepare( $sql, $values ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared

		if ( 'pdf' === $format ) {
			VI_Export::stream_pdf( $rows );
		} else {
			VI_Export::stream_csv( $rows );
		}
		exit;
	}

	public function register_menu() {
		add_menu_page(
			__( 'Visitors', 'visitor-insights' ),
			__( 'Visitors', 'visitor-insights' ),
			'manage_options',
			self::PAGE_SLUG,
			array( $this, 'render_page' ),
			'dashicons-groups',
			26
		);
	}

	public function enqueue_assets( $hook ) {
		if ( 'toplevel_page_' . self::PAGE_SLUG !== $hook ) {
			return;
		}

		wp_enqueue_style( 'vi-admin', VI_PLUGIN_URL . 'assets/css/admin.css', array(), VI_VERSION );
		wp_enqueue_script( 'vi-admin', VI_PLUGIN_URL . 'assets/js/admin.js', array(), VI_VERSION, true );

		wp_localize_script(
			'vi-admin',
			'VI_ADMIN',
			array(
				'restUrl'    => esc_url_raw( rest_url( 'visitor-insights/v1' ) ),
				'nonce'      => wp_create_nonce( 'wp_rest' ),
				'exportUrl'  => admin_url( 'admin-post.php' ),
				'exportNonce' => wp_create_nonce( 'vi_export' ),
				'i18n'    => array(
					'confirmExport' => __( 'Export the currently filtered sessions?', 'visitor-insights' ),
					'skipTraceOff'  => __( 'Skip trace is disabled. Enable it and acknowledge the consent notice below first.', 'visitor-insights' ),
				),
			)
		);
	}

	public function render_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		?>
		<div class="wrap vi-wrap">
			<h1><?php esc_html_e( 'Visitors', 'visitor-insights' ); ?></h1>

			<div id="vi-stats" class="vi-stats-grid" aria-live="polite"></div>

			<div class="vi-toolbar">
				<input type="text" id="vi-search" placeholder="<?php esc_attr_e( 'Search IP, country, city, referrer…', 'visitor-insights' ); ?>" />
				<select id="vi-source-filter">
					<?php foreach ( VI_Source::choices() as $key => $label ) : ?>
						<option value="<?php echo esc_attr( $key ); ?>"><?php echo esc_html( $label ); ?></option>
					<?php endforeach; ?>
				</select>
				<label><?php esc_html_e( 'From', 'visitor-insights' ); ?> <input type="date" id="vi-date-from" /></label>
				<label><?php esc_html_e( 'To', 'visitor-insights' ); ?> <input type="date" id="vi-date-to" /></label>
				<button type="button" class="button" id="vi-filter-apply"><?php esc_html_e( 'Apply', 'visitor-insights' ); ?></button>
				<span class="vi-toolbar-spacer"></span>
				<button type="button" class="button" id="vi-export-csv"><?php esc_html_e( 'Download CSV', 'visitor-insights' ); ?></button>
				<button type="button" class="button" id="vi-export-pdf"><?php esc_html_e( 'Download PDF', 'visitor-insights' ); ?></button>
			</div>
			<p class="description">
				<?php esc_html_e( 'Google Ads is detected from Google\'s own click-id parameters (gclid, gbraid, etc.) and is always reliable. Facebook Ads detection additionally needs UTM tagging on the ad (utm_medium=cpc/paid) to tell ads apart from organic Facebook/Instagram clicks - add UTM parameters in Meta Ads Manager for accurate Facebook Ads filtering.', 'visitor-insights' ); ?>
			</p>

			<table class="widefat striped vi-sessions-table">
				<thead>
					<tr>
						<th><?php esc_html_e( 'Location', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'IP / Network', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'Source', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'Landing Page / Referrer', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'Views', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'Last Seen', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'Identified', 'visitor-insights' ); ?></th>
						<th><?php esc_html_e( 'Skip Trace', 'visitor-insights' ); ?></th>
					</tr>
				</thead>
				<tbody id="vi-sessions-body">
					<tr><td colspan="8"><?php esc_html_e( 'Loading…', 'visitor-insights' ); ?></td></tr>
				</tbody>
			</table>
			<div id="vi-pagination" class="vi-pagination"></div>

			<div id="vi-skiptrace-modal" class="vi-modal" hidden>
				<div class="vi-modal-content">
					<h2><?php esc_html_e( 'Run Skip Trace', 'visitor-insights' ); ?></h2>
					<p class="description"><?php esc_html_e( 'Provide at least one identifying detail (name, address, phone, or email) captured elsewhere for this visitor.', 'visitor-insights' ); ?></p>
					<div class="vi-form-grid">
						<input type="hidden" id="vi-st-session-id" />
						<input type="text" id="vi-st-name" placeholder="<?php esc_attr_e( 'Full name', 'visitor-insights' ); ?>" />
						<input type="text" id="vi-st-address" placeholder="<?php esc_attr_e( 'Street address', 'visitor-insights' ); ?>" />
						<input type="text" id="vi-st-city" placeholder="<?php esc_attr_e( 'City', 'visitor-insights' ); ?>" />
						<input type="text" id="vi-st-state" placeholder="<?php esc_attr_e( 'State', 'visitor-insights' ); ?>" />
						<input type="text" id="vi-st-zip" placeholder="<?php esc_attr_e( 'ZIP', 'visitor-insights' ); ?>" />
						<input type="text" id="vi-st-phone" placeholder="<?php esc_attr_e( 'Phone', 'visitor-insights' ); ?>" />
						<input type="email" id="vi-st-email" placeholder="<?php esc_attr_e( 'Email', 'visitor-insights' ); ?>" />
					</div>
					<div id="vi-st-results" class="vi-st-results"></div>
					<div class="vi-modal-actions">
						<button type="button" class="button button-primary" id="vi-st-run"><?php esc_html_e( 'Run Skip Trace', 'visitor-insights' ); ?></button>
						<button type="button" class="button" id="vi-st-close"><?php esc_html_e( 'Close', 'visitor-insights' ); ?></button>
					</div>
				</div>
			</div>

			<h2 style="margin-top:2.5em;"><?php esc_html_e( 'Settings', 'visitor-insights' ); ?></h2>
			<form action="options.php" method="post">
				<?php
				settings_fields( 'vi_settings_group' );
				do_settings_sections( self::PAGE_SLUG );
				submit_button();
				?>
			</form>
		</div>
		<?php
	}

	public function register_settings() {
		register_setting( 'vi_settings_group', 'vi_retention_days', array( 'sanitize_callback' => 'absint', 'default' => 365 ) );
		register_setting( 'vi_settings_group', 'vi_track_logged_in_admins', array( 'sanitize_callback' => array( $this, 'sanitize_checkbox' ) ) );
		register_setting( 'vi_settings_group', 'vi_geo_lookup_enabled', array( 'sanitize_callback' => array( $this, 'sanitize_checkbox' ) ) );
		register_setting( 'vi_settings_group', 'vi_excluded_paths', array( 'sanitize_callback' => 'sanitize_text_field' ) );
		register_setting( 'vi_settings_group', 'vi_skip_trace_enabled', array( 'sanitize_callback' => array( $this, 'sanitize_checkbox' ) ) );
		register_setting( 'vi_settings_group', 'vi_skip_trace_consent_ack', array( 'sanitize_callback' => array( $this, 'sanitize_checkbox' ) ) );
		register_setting( 'vi_settings_group', 'vi_apify_token', array( 'sanitize_callback' => array( $this, 'sanitize_apify_token' ) ) );

		add_settings_section( 'vi_tracking_section', __( 'Tracking', 'visitor-insights' ), '__return_false', self::PAGE_SLUG );

		add_settings_field(
			'vi_retention_days',
			__( 'Keep data for (days)', 'visitor-insights' ),
			function () {
				printf(
					'<input type="number" min="0" name="vi_retention_days" value="%d" class="small-text" /> <p class="description">%s</p>',
					absint( get_option( 'vi_retention_days', 365 ) ),
					esc_html__( '0 = keep forever. Older sessions are purged daily.', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_tracking_section'
		);

		add_settings_field(
			'vi_track_logged_in_admins',
			__( 'Track logged-in admins', 'visitor-insights' ),
			function () {
				printf(
					'<label><input type="checkbox" name="vi_track_logged_in_admins" value="1" %s /> %s</label>',
					checked( get_option( 'vi_track_logged_in_admins', false ), true, false ),
					esc_html__( 'Include your own logged-in visits in the stats (off by default to avoid inflating them).', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_tracking_section'
		);

		add_settings_field(
			'vi_geo_lookup_enabled',
			__( 'IP geolocation', 'visitor-insights' ),
			function () {
				printf(
					'<label><input type="checkbox" name="vi_geo_lookup_enabled" value="1" %s /> %s</label>',
					checked( get_option( 'vi_geo_lookup_enabled', true ), true, false ),
					esc_html__( 'Look up country/region/city/ISP for new sessions via ip-api.com (free tier, no key required).', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_tracking_section'
		);

		add_settings_field(
			'vi_excluded_paths',
			__( 'Excluded path prefixes', 'visitor-insights' ),
			function () {
				printf(
					'<input type="text" name="vi_excluded_paths" value="%s" class="regular-text" placeholder="/checkout, /my-account" /> <p class="description">%s</p>',
					esc_attr( get_option( 'vi_excluded_paths', '' ) ),
					esc_html__( 'Comma-separated path prefixes to never track.', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_tracking_section'
		);

		add_settings_section(
			'vi_skiptrace_section',
			__( 'Skip Trace (optional)', 'visitor-insights' ),
			function () {
				echo '<p>' . esc_html__( 'Skip trace looks up a visitor\'s name/phone/email from a seed identity you already have via a third-party data provider (Apify). This can carry real privacy/legal obligations depending on your jurisdiction and how you use it - review your local requirements before enabling.', 'visitor-insights' ) . '</p>';
			},
			self::PAGE_SLUG
		);

		add_settings_field(
			'vi_apify_token',
			__( 'Apify API Token', 'visitor-insights' ),
			function () {
				$has_token = (bool) get_option( 'vi_apify_token', '' );
				printf(
					'<input type="password" name="vi_apify_token" value="" autocomplete="off" class="regular-text" placeholder="%s" /> <p class="description">%s <a href="https://console.apify.com/account/integrations" target="_blank" rel="noopener noreferrer">%s</a></p>',
					$has_token ? esc_attr__( '••••••••••••••••  (leave blank to keep)', 'visitor-insights' ) : esc_attr__( 'Not configured', 'visitor-insights' ),
					esc_html__( 'Leave blank to keep the current token.', 'visitor-insights' ),
					esc_html__( 'Get a token', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_skiptrace_section'
		);

		add_settings_field(
			'vi_skip_trace_enabled',
			__( 'Enable skip trace', 'visitor-insights' ),
			function () {
				printf(
					'<label><input type="checkbox" name="vi_skip_trace_enabled" value="1" %s /> %s</label>',
					checked( get_option( 'vi_skip_trace_enabled', false ), true, false ),
					esc_html__( 'Show the Skip Trace action on the Visitors page.', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_skiptrace_section'
		);

		add_settings_field(
			'vi_skip_trace_consent_ack',
			__( 'Consent acknowledgement', 'visitor-insights' ),
			function () {
				printf(
					'<label><input type="checkbox" name="vi_skip_trace_consent_ack" value="1" %s /> %s</label>',
					checked( get_option( 'vi_skip_trace_consent_ack', false ), true, false ),
					esc_html__( 'I have reviewed the privacy/legal requirements for identity lookups in my jurisdiction and accept responsibility for how this feature is used on this site.', 'visitor-insights' )
				);
			},
			self::PAGE_SLUG,
			'vi_skiptrace_section'
		);
	}

	public function sanitize_checkbox( $value ) {
		return ! empty( $value );
	}

	/**
	 * Blank submission keeps the existing token (the field is never
	 * re-populated with the real secret, so "blank" always means
	 * "unchanged" rather than "clear it").
	 */
	public function sanitize_apify_token( $value ) {
		$value = trim( (string) $value );
		if ( '' === $value ) {
			return get_option( 'vi_apify_token', '' );
		}
		return sanitize_text_field( $value );
	}
}
