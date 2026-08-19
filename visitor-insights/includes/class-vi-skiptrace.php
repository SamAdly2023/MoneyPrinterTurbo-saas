<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Optional Apify-powered skip trace: given a seed identity (name/address/
 * phone/email), returns matching people records (additional phones,
 * emails, address history) from the "one-api/skip-trace" Apify actor.
 *
 * This does NOT reverse-lookup an IP or device fingerprint - it needs a
 * human-provided seed. It's gated behind two separate site-owner opt-ins
 * (vi_skip_trace_enabled + vi_skip_trace_consent_ack) because running
 * identity lookups on site visitors carries real privacy/legal exposure
 * (GDPR/CCPA-style rules) that varies by jurisdiction and use case - the
 * site owner must consciously turn this on, it is never on by default.
 */
class VI_Skiptrace {

	const ACTOR_ID = 'one-api~skip-trace';

	public static function is_configured() {
		return (bool) get_option( 'vi_apify_token', '' );
	}

	public static function is_enabled() {
		return self::is_configured()
			&& get_option( 'vi_skip_trace_enabled', false )
			&& get_option( 'vi_skip_trace_consent_ack', false );
	}

	/**
	 * @param array $seed { name, address, city, state, zip, phone, email }
	 * @return array|WP_Error List of matched records, or WP_Error on failure.
	 */
	public static function run( array $seed, $max_results = 3 ) {
		if ( ! self::is_enabled() ) {
			return new WP_Error(
				'vi_skip_trace_disabled',
				__( 'Skip trace is not enabled. Configure an Apify token and acknowledge the consent notice in Visitors → Settings first.', 'visitor-insights' )
			);
		}

		$token = get_option( 'vi_apify_token', '' );

		$name    = sanitize_text_field( $seed['name'] ?? '' );
		$address = sanitize_text_field( $seed['address'] ?? '' );
		$city    = sanitize_text_field( $seed['city'] ?? '' );
		$state   = sanitize_text_field( $seed['state'] ?? '' );
		$zip     = sanitize_text_field( $seed['zip'] ?? '' );
		$phone   = sanitize_text_field( $seed['phone'] ?? '' );
		$email   = sanitize_email( $seed['email'] ?? '' );

		$csz = trim( implode( ' ', array_filter( array( implode( ', ', array_filter( array( $city, $state ) ) ), $zip ) ) ) );

		$input = array(
			'max_results' => max( 1, min( absint( $max_results ), 10 ) ),
		);

		if ( $name ) {
			$input['name'] = array( $csz ? "{$name}; {$csz}" : $name );
		}
		if ( $address ) {
			$input['street_citystatezip'] = array( $csz ? "{$address}; {$csz}" : $address );
		}
		if ( $phone ) {
			$input['phone_number'] = array( $phone );
		}
		if ( $email ) {
			$input['email'] = array( $email );
		}

		if ( empty( $input['name'] ) && empty( $input['street_citystatezip'] ) && empty( $input['phone_number'] ) && empty( $input['email'] ) ) {
			return new WP_Error(
				'vi_skip_trace_missing_seed',
				__( 'Skip trace requires at least a name, address, phone, or email.', 'visitor-insights' )
			);
		}

		$url = sprintf(
			'https://api.apify.com/v2/acts/%s/run-sync-get-dataset-items?token=%s',
			self::ACTOR_ID,
			rawurlencode( $token )
		);

		$response = wp_remote_post(
			$url,
			array(
				'headers' => array( 'Content-Type' => 'application/json' ),
				'body'    => wp_json_encode( $input ),
				'timeout' => 120, // Skip traces can take a while.
			)
		);

		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = wp_remote_retrieve_response_code( $response );
		if ( $code < 200 || $code >= 300 ) {
			return new WP_Error(
				'vi_skip_trace_http_error',
				sprintf(
					/* translators: %d: HTTP status code */
					__( 'Apify request failed (HTTP %d).', 'visitor-insights' ),
					$code
				)
			);
		}

		$data = json_decode( wp_remote_retrieve_body( $response ), true );

		return is_array( $data ) ? $data : array();
	}
}
