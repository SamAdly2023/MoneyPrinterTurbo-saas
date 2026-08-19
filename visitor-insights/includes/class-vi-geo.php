<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Free IP geolocation via ip-api.com, with per-IP transient caching so a
 * busy site doesn't blow through ip-api's free-tier rate limit (45
 * requests/minute) re-looking-up IPs it already knows about. A given IP's
 * geo data essentially never changes, so a long cache TTL is safe.
 */
class VI_Geo {

	const CACHE_TTL = 30 * DAY_IN_SECONDS;

	/**
	 * @param string $ip
	 * @return array{country:string,region:string,city:string,zip:string,lat:?float,lng:?float,isp:string,org:string,is_mobile:bool,is_proxy:bool,is_hosting:bool}
	 */
	public static function lookup( $ip ) {
		$empty = array(
			'country'    => '',
			'region'     => '',
			'city'       => '',
			'zip'        => '',
			'lat'        => null,
			'lng'        => null,
			'isp'        => '',
			'org'        => '',
			'is_mobile'  => false,
			'is_proxy'   => false,
			'is_hosting' => false,
		);

		if ( ! get_option( 'vi_geo_lookup_enabled', true ) ) {
			return $empty;
		}

		if ( empty( $ip ) || ! self::is_public_ip( $ip ) ) {
			return $empty;
		}

		$cache_key = 'vi_geo_' . md5( $ip );
		$cached    = get_transient( $cache_key );
		if ( false !== $cached ) {
			return $cached;
		}

		$fields = 'status,message,country,regionName,city,zip,lat,lon,isp,org,mobile,proxy,hosting';
		$url    = sprintf( 'http://ip-api.com/json/%s?fields=%s', rawurlencode( $ip ), $fields );

		$response = wp_remote_get(
			$url,
			array(
				'timeout' => 5,
			)
		);

		if ( is_wp_error( $response ) || 200 !== wp_remote_retrieve_response_code( $response ) ) {
			set_transient( $cache_key, $empty, HOUR_IN_SECONDS );
			return $empty;
		}

		$body = json_decode( wp_remote_retrieve_body( $response ), true );
		if ( ! is_array( $body ) || 'success' !== ( $body['status'] ?? '' ) ) {
			set_transient( $cache_key, $empty, HOUR_IN_SECONDS );
			return $empty;
		}

		$result = array(
			'country'    => sanitize_text_field( $body['country'] ?? '' ),
			'region'     => sanitize_text_field( $body['regionName'] ?? '' ),
			'city'       => sanitize_text_field( $body['city'] ?? '' ),
			'zip'        => sanitize_text_field( $body['zip'] ?? '' ),
			'lat'        => isset( $body['lat'] ) ? (float) $body['lat'] : null,
			'lng'        => isset( $body['lon'] ) ? (float) $body['lon'] : null,
			'isp'        => sanitize_text_field( $body['isp'] ?? '' ),
			'org'        => sanitize_text_field( $body['org'] ?? '' ),
			'is_mobile'  => ! empty( $body['mobile'] ),
			'is_proxy'   => ! empty( $body['proxy'] ),
			'is_hosting' => ! empty( $body['hosting'] ),
		);

		set_transient( $cache_key, $result, self::CACHE_TTL );

		return $result;
	}

	/**
	 * Skip lookups for loopback/private/reserved ranges (localhost, LAN,
	 * Docker bridge networks) - ip-api would just return a useless/empty
	 * result and it's a wasted request.
	 */
	private static function is_public_ip( $ip ) {
		return false !== filter_var(
			$ip,
			FILTER_VALIDATE_IP,
			FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE
		);
	}
}
