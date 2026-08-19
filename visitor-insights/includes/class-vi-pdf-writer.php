<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * A minimal, dependency-free PDF writer. No Composer, no vendored library,
 * no external calls - just the raw PDF object format, built by hand, so
 * PDF export works the moment the plugin is installed (some hosts only
 * give admins the WordPress dashboard itself, with no way to run
 * `composer install` or upload extra files).
 *
 * Deliberately minimal: one base-14 font (Helvetica, no embedding needed
 * since every PDF reader ships it), landscape A4, simple text rows for a
 * tabular report. Not a general-purpose PDF library.
 */
class VI_PDF_Writer {

	const PAGE_WIDTH  = 842; // A4 landscape, points.
	const PAGE_HEIGHT = 595;
	const MARGIN      = 36;
	const ROW_HEIGHT  = 14;
	const ROWS_PER_PAGE = 30;

	private $columns; // array of [label, width]
	private $objects = array(); // 1-indexed object bodies, built in order.
	private $font_regular_id;
	private $font_bold_id;

	public function __construct( array $columns ) {
		$this->columns = $columns;
	}

	/**
	 * @param string $title
	 * @param string $subtitle
	 * @param array  $rows Each row is an array of string cells, same order as $columns.
	 * @return string Raw PDF file bytes.
	 */
	public function build( $title, $subtitle, array $rows ) {
		// Reserve object 1 = Catalog, 2 = Pages (filled in once we know page ids).
		$this->objects[1] = null;
		$this->objects[2] = null;

		$this->font_regular_id = $this->add_object( "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>" );
		$this->font_bold_id    = $this->add_object( "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>" );

		$page_ids = array();
		$chunks   = array_chunk( $rows, self::ROWS_PER_PAGE );
		if ( empty( $chunks ) ) {
			$chunks = array( array() );
		}

		foreach ( $chunks as $index => $chunk ) {
			$is_first = ( 0 === $index );
			$stream   = $this->render_page_stream( $is_first ? $title : '', $is_first ? $subtitle : '', $chunk, $is_first );
			$page_ids[] = $this->add_page( $stream );
		}

		$this->objects[2] = sprintf(
			'<< /Type /Pages /Kids [%s] /Count %d >>',
			implode( ' ', array_map( function ( $id ) { return "{$id} 0 R"; }, $page_ids ) ),
			count( $page_ids )
		);
		$this->objects[1] = '<< /Type /Catalog /Pages 2 0 R >>';

		return $this->assemble();
	}

	private function add_object( $body ) {
		$this->objects[] = $body;
		return count( $this->objects );
	}

	private function add_page( $content_stream ) {
		$stream_id = $this->add_object( self::stream_object( $content_stream ) );
		$page_body = sprintf(
			'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>',
			self::PAGE_WIDTH,
			self::PAGE_HEIGHT,
			$this->font_regular_id,
			$this->font_bold_id,
			$stream_id
		);
		return $this->add_object( $page_body );
	}

	private static function stream_object( $content ) {
		return sprintf( "<< /Length %d >>\nstream\n%s\nendstream", strlen( $content ), $content );
	}

	private function render_page_stream( $title, $subtitle, array $rows, $with_header ) {
		$x = self::MARGIN;
		$y = self::PAGE_HEIGHT - self::MARGIN;
		$ops = array();

		if ( $title ) {
			$ops[] = $this->text_op( 'F2', 16, $x, $y, $title );
			$y    -= 20;
		}
		if ( $subtitle ) {
			$ops[] = $this->text_op( 'F1', 9, $x, $y, $subtitle );
			$y    -= 20;
		}

		if ( $with_header || $title || $subtitle ) {
			$y -= 4;
		}

		// Column headers.
		$cx = $x;
		foreach ( $this->columns as $col ) {
			$ops[] = $this->text_op( 'F2', 9, $cx, $y, $col[0] );
			$cx   += $col[1];
		}
		$y -= self::ROW_HEIGHT + 2;

		foreach ( $rows as $row ) {
			$cx = $x;
			foreach ( $row as $i => $cell ) {
				$width = $this->columns[ $i ][1] ?? 80;
				$ops[] = $this->text_op( 'F1', 8, $cx, $y, self::fit_text( (string) $cell, $width ) );
				$cx   += $width;
			}
			$y -= self::ROW_HEIGHT;
		}

		return implode( "\n", $ops );
	}

	private function text_op( $font, $size, $x, $y, $text ) {
		return sprintf( "BT\n/%s %d Tf\n1 0 0 1 %d %d Tm\n(%s) Tj\nET", $font, $size, $x, $y, self::escape_text( $text ) );
	}

	/**
	 * Rough character-count clip so a long cell doesn't overrun into the
	 * next column - Helvetica isn't monospace, so this is an approximation
	 * (about 1.8pt per character at 8pt size), not exact typesetting.
	 */
	private static function fit_text( $text, $column_width ) {
		$max_chars = max( 4, (int) ( $column_width / 4.4 ) );
		if ( function_exists( 'mb_strlen' ) && mb_strlen( $text ) > $max_chars ) {
			return mb_substr( $text, 0, $max_chars - 1 ) . '…';
		}
		return $text;
	}

	private static function escape_text( $text ) {
		// PDF literal strings only need \, ( and ) escaped. Strip anything
		// outside printable Latin-1 range rather than attempt font
		// encoding we don't embed.
		$text = preg_replace( '/[^\x20-\x7E]/', '', (string) $text );
		return str_replace( array( '\\', '(', ')' ), array( '\\\\', '\\(', '\\)' ), $text );
	}

	private function assemble() {
		$out    = "%PDF-1.4\n";
		$offsets = array();

		foreach ( $this->objects as $id => $body ) {
			$offsets[ $id ] = strlen( $out );
			$out           .= "{$id} 0 obj\n{$body}\nendobj\n";
		}

		$xref_offset = strlen( $out );
		$count       = count( $this->objects ) + 1;

		$out .= "xref\n0 {$count}\n";
		$out .= "0000000000 65535 f \n";
		for ( $id = 1; $id <= count( $this->objects ); $id++ ) {
			$out .= sprintf( "%010d 00000 n \n", $offsets[ $id ] );
		}

		$out .= "trailer\n<< /Size {$count} /Root 1 0 R >>\nstartxref\n{$xref_offset}\n%%EOF";

		return $out;
	}
}
