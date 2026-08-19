<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * CSV export is native PHP. PDF export uses the bundled dependency-free
 * VI_PDF_Writer rather than a Composer library - many WordPress admins
 * only have the wp-admin dashboard itself (no SSH/FTP/composer access),
 * so PDF export needs to work immediately after installing the plugin
 * with nothing extra to run.
 */
class VI_Export {

	public static function stream_csv( array $rows ) {
		nocache_headers();
		header( 'Content-Type: text/csv; charset=utf-8' );
		header( 'Content-Disposition: attachment; filename="visitor-report-' . gmdate( 'Y-m-d' ) . '.csv"' );

		$out = fopen( 'php://output', 'w' );

		fputcsv(
			$out,
			array(
				'Session ID', 'IP', 'Source', 'Country', 'Region', 'City', 'ISP',
				'Mobile', 'Proxy', 'Hosting', 'Referrer', 'Landing Page',
				'UTM Source', 'UTM Medium', 'UTM Campaign',
				'First Seen (UTC)', 'Last Seen (UTC)', 'Page Views', 'Identified',
			)
		);

		foreach ( $rows as $row ) {
			fputcsv(
				$out,
				array(
					$row['session_id'],
					$row['ip'],
					VI_Source::label( $row ),
					$row['country'],
					$row['region'],
					$row['city'],
					$row['isp'],
					$row['is_mobile'] ? 'Yes' : 'No',
					$row['is_proxy'] ? 'Yes' : 'No',
					$row['is_hosting'] ? 'Yes' : 'No',
					$row['referrer'],
					$row['landing_page'],
					$row['utm_source'],
					$row['utm_medium'],
					$row['utm_campaign'],
					$row['first_seen'],
					$row['last_seen'],
					$row['page_count'],
					$row['identified'] ? 'Yes' : 'No',
				)
			);
		}

		fclose( $out );
	}

	public static function stream_pdf( array $rows ) {
		$site_name    = get_bloginfo( 'name' );
		$generated_at = current_time( 'Y-m-d H:i' ) . ' UTC';
		$subtitle     = sprintf(
			/* translators: 1: generation timestamp, 2: number of sessions */
			__( 'Generated %1$s · %2$d sessions', 'visitor-insights' ),
			$generated_at,
			count( $rows )
		);

		$columns = array(
			array( 'IP', 75 ),
			array( 'Location', 110 ),
			array( 'Source', 85 ),
			array( 'Referrer', 115 ),
			array( 'Landing Page', 115 ),
			array( 'Views', 35 ),
			array( 'First Seen', 80 ),
			array( 'Last Seen', 80 ),
			array( 'ID', 35 ),
		);

		$table_rows = array();
		foreach ( $rows as $row ) {
			$location     = trim( implode( ', ', array_filter( array( $row['city'], $row['region'], $row['country'] ) ) ) );
			$table_rows[] = array(
				$row['ip'],
				$location,
				VI_Source::label( $row ),
				$row['referrer'],
				$row['landing_page'],
				$row['page_count'],
				$row['first_seen'],
				$row['last_seen'],
				$row['identified'] ? 'Yes' : 'No',
			);
		}

		$writer = new VI_PDF_Writer( $columns );
		$pdf    = $writer->build( $site_name . ' - Visitor Report', $subtitle, $table_rows );

		nocache_headers();
		header( 'Content-Type: application/pdf' );
		header( 'Content-Disposition: attachment; filename="visitor-report-' . gmdate( 'Y-m-d' ) . '.pdf"' );
		header( 'Content-Length: ' . strlen( $pdf ) );
		echo $pdf; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- raw binary PDF stream, not HTML.
	}
}
