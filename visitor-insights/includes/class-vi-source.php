<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Classifies a session's traffic source from its landing page query string,
 * referrer, and UTM fields - both for the filter dropdown (SQL conditions)
 * and for the human-readable "Source" column.
 *
 * Google Ads is a reliable signal: gclid/gbraid/wbraid/gad_* parameters are
 * appended ONLY by Google Ads, never by organic search results, so their
 * presence alone is conclusive.
 *
 * Facebook is murkier: fbclid is appended to any link clicked from inside
 * Facebook/Instagram - ads and organic post shares alike - so it can't
 * distinguish "ad" from "organic" on its own. Meta Ads Manager supports
 * adding UTM parameters to an ad's URL (Dynamic UTM parameters), so when
 * present we use utm_medium to tell them apart; without that tagging on
 * the ad, fbclid traffic is reported as "Facebook / Instagram (Social)"
 * rather than a false-confidence "Facebook Ads".
 */
class VI_Source {

	const ALL             = '';
	const GOOGLE_ADS      = 'google_ads';
	const FACEBOOK_ADS    = 'facebook_ads';
	const GOOGLE_ORGANIC  = 'google_organic';
	const SOCIAL_ORGANIC  = 'social_organic';
	const DIRECT          = 'direct';

	private static $paid_mediums = array( 'cpc', 'ppc', 'paid', 'ads', 'paidsocial', 'paidsearch' );

	public static function choices() {
		return array(
			self::ALL            => __( 'All Sources', 'visitor-insights' ),
			self::GOOGLE_ADS     => __( 'Google Ads', 'visitor-insights' ),
			self::FACEBOOK_ADS   => __( 'Facebook / Instagram Ads', 'visitor-insights' ),
			self::GOOGLE_ORGANIC => __( 'Google (organic search)', 'visitor-insights' ),
			self::SOCIAL_ORGANIC => __( 'Facebook / Instagram (organic)', 'visitor-insights' ),
			self::DIRECT         => __( 'Direct / no referrer', 'visitor-insights' ),
		);
	}

	/**
	 * Raw SQL fragment for a given filter key, or '' for no filter. Every
	 * value here is a fixed literal (no interpolated user input), so it's
	 * safe to inline directly into the WHERE clause built in
	 * VI_REST_Controller::build_filters() - EXCEPT that this fragment
	 * always gets passed through $wpdb->prepare() downstream (for the
	 * LIMIT/OFFSET placeholders), which treats any bare "%" as the start
	 * of a printf-style placeholder. Every literal "%" wildcard in these
	 * LIKE patterns must be doubled to "%%" so prepare() reduces it back
	 * to a single "%" instead of corrupting the query/throwing a warning.
	 */
	public static function sql_condition( $key ) {
		$medium_list = "'" . implode( "','", self::$paid_mediums ) . "'";

		switch ( $key ) {
			case self::GOOGLE_ADS:
				return "(landing_page LIKE '%%gclid=%%' OR landing_page LIKE '%%gad_source%%' OR landing_page LIKE '%%gad_campaignid%%' OR landing_page LIKE '%%gbraid=%%' OR landing_page LIKE '%%wbraid=%%' OR (utm_source IN ('google','adwords','google_ads') AND utm_medium IN ({$medium_list})))";

			case self::FACEBOOK_ADS:
				return "((landing_page LIKE '%%fbclid=%%' AND utm_medium IN ({$medium_list})) OR (utm_source IN ('facebook','fb','meta','instagram','ig') AND utm_medium IN ({$medium_list})))";

			case self::GOOGLE_ORGANIC:
				return "(referrer LIKE '%%google.%%' AND landing_page NOT LIKE '%%gclid=%%' AND landing_page NOT LIKE '%%gad_source%%' AND landing_page NOT LIKE '%%gbraid=%%' AND landing_page NOT LIKE '%%wbraid=%%')";

			case self::SOCIAL_ORGANIC:
				return "((landing_page LIKE '%%fbclid=%%' OR referrer LIKE '%%facebook.%%' OR referrer LIKE '%%instagram.%%') AND NOT (utm_medium IN ({$medium_list})))";

			case self::DIRECT:
				return "(referrer = '')";

			default:
				return '';
		}
	}

	/**
	 * Human-readable label for one session row, for the table/CSV/PDF.
	 */
	public static function label( array $row ) {
		$landing_page = $row['landing_page'] ?? '';
		$referrer     = $row['referrer'] ?? '';
		$utm_source   = strtolower( $row['utm_source'] ?? '' );
		$utm_medium   = strtolower( $row['utm_medium'] ?? '' );
		$is_paid      = in_array( $utm_medium, self::$paid_mediums, true );

		$has_google_ads_param = (bool) preg_match( '/[?&](gclid|gad_source|gad_campaignid|gbraid|wbraid)=/', $landing_page );
		if ( $has_google_ads_param || ( in_array( $utm_source, array( 'google', 'adwords', 'google_ads' ), true ) && $is_paid ) ) {
			return __( 'Google Ads', 'visitor-insights' );
		}

		$has_fbclid = (bool) preg_match( '/[?&]fbclid=/', $landing_page );
		$is_meta_utm = in_array( $utm_source, array( 'facebook', 'fb', 'meta', 'instagram', 'ig' ), true );
		if ( ( $has_fbclid && $is_paid ) || ( $is_meta_utm && $is_paid ) ) {
			return __( 'Facebook Ads', 'visitor-insights' );
		}
		if ( $has_fbclid || stripos( $referrer, 'facebook.' ) !== false || stripos( $referrer, 'instagram.' ) !== false ) {
			return __( 'Facebook / Instagram', 'visitor-insights' );
		}

		if ( stripos( $referrer, 'google.' ) !== false ) {
			return __( 'Google (organic)', 'visitor-insights' );
		}

		if ( '' === $referrer ) {
			return __( 'Direct', 'visitor-insights' );
		}

		$host = wp_parse_url( $referrer, PHP_URL_HOST );
		return $host ? $host : __( 'Referral', 'visitor-insights' );
	}
}
