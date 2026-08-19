(function () {
	'use strict';

	if (typeof VI_ADMIN === 'undefined') {
		return;
	}

	var state = { page: 1, perPage: 50, search: '', dateFrom: '', dateTo: '', source: '' };

	function escapeHtml(value) {
		var div = document.createElement('div');
		div.textContent = value === null || value === undefined ? '' : String(value);
		return div.innerHTML;
	}

	function apiFetch(path, options) {
		options = options || {};
		options.headers = Object.assign({ 'X-WP-Nonce': VI_ADMIN.nonce }, options.headers || {});
		return fetch(VI_ADMIN.restUrl + path, options).then(function (res) {
			return res.json().then(function (json) {
				if (!res.ok) {
					throw new Error(json && json.error ? json.error : 'Request failed');
				}
				return json;
			});
		});
	}

	function filterQuery() {
		var params = new URLSearchParams();
		if (state.search) params.set('search', state.search);
		if (state.dateFrom) params.set('date_from', state.dateFrom);
		if (state.dateTo) params.set('date_to', state.dateTo);
		if (state.source) params.set('source', state.source);
		return params;
	}

	function loadStats() {
		apiFetch('/stats?' + filterQuery().toString()).then(function (data) {
			var el = document.getElementById('vi-stats');
			if (!el) return;
			var tiles = [
				{ label: 'Sessions', value: data.sessions },
				{ label: 'Page Views', value: data.pageviews },
				{ label: 'Countries', value: data.countries },
				{ label: 'Identified', value: data.identified }
			];
			el.innerHTML = tiles.map(function (t) {
				return '<div class="vi-stat-tile"><div class="vi-stat-value">' + escapeHtml(t.value) + '</div><div class="vi-stat-label">' + escapeHtml(t.label) + '</div></div>';
			}).join('');
		}).catch(function () {});
	}

	function loadSessions() {
		var body = document.getElementById('vi-sessions-body');
		if (!body) return;
		body.innerHTML = '<tr><td colspan="8">Loading…</td></tr>';

		var query = filterQuery();
		query.set('page', state.page);
		query.set('per_page', state.perPage);

		apiFetch('/sessions?' + query.toString()).then(function (data) {
			if (!data.sessions.length) {
				body.innerHTML = '<tr><td colspan="8">No visitors match these filters yet.</td></tr>';
				renderPagination(data);
				return;
			}

			body.innerHTML = data.sessions.map(renderRow).join('');
			renderPagination(data);

			body.querySelectorAll('.vi-skip-trace-btn').forEach(function (btn) {
				btn.addEventListener('click', function () {
					openSkipTraceModal(btn.getAttribute('data-session-id'));
				});
			});
		}).catch(function (err) {
			body.innerHTML = '<tr><td colspan="8">Failed to load: ' + escapeHtml(err.message) + '</td></tr>';
		});
	}

	function renderRow(row) {
		var location = [row.city, row.region, row.country].filter(Boolean).join(', ') || '—';
		var network = [row.ip, row.isp].filter(Boolean).join(' · ');
		var flags = [];
		if (Number(row.is_mobile)) flags.push('mobile');
		if (Number(row.is_proxy)) flags.push('proxy');
		if (Number(row.is_hosting)) flags.push('hosting');
		if (flags.length) network += ' (' + flags.join(', ') + ')';

		return (
			'<tr>' +
			'<td>' + escapeHtml(location) + '</td>' +
			'<td>' + escapeHtml(network) + '</td>' +
			'<td>' + escapeHtml(row.source_label) + '</td>' +
			'<td><div>' + escapeHtml(row.landing_page) + '</div><div class="vi-muted">' + escapeHtml(row.referrer || 'Direct') + '</div></td>' +
			'<td>' + escapeHtml(row.page_count) + '</td>' +
			'<td>' + escapeHtml(row.last_seen) + '</td>' +
			'<td>' + (Number(row.identified) ? '✅' : '—') + '</td>' +
			'<td><button type="button" class="button button-small vi-skip-trace-btn" data-session-id="' + escapeHtml(row.session_id) + '">Skip Trace</button></td>' +
			'</tr>'
		);
	}

	function renderPagination(data) {
		var el = document.getElementById('vi-pagination');
		if (!el) return;
		var totalPages = Math.max(1, Math.ceil(data.total / data.per_page));
		el.innerHTML =
			'<button type="button" class="button" id="vi-prev" ' + (state.page <= 1 ? 'disabled' : '') + '>&laquo; Prev</button> ' +
			'<span class="vi-page-info">Page ' + state.page + ' of ' + totalPages + ' (' + data.total + ' sessions)</span> ' +
			'<button type="button" class="button" id="vi-next" ' + (state.page >= totalPages ? 'disabled' : '') + '>Next &raquo;</button>';

		var prev = document.getElementById('vi-prev');
		var next = document.getElementById('vi-next');
		if (prev) prev.addEventListener('click', function () { state.page = Math.max(1, state.page - 1); loadSessions(); });
		if (next) next.addEventListener('click', function () { state.page = state.page + 1; loadSessions(); });
	}

	function openSkipTraceModal(sessionId) {
		var modal = document.getElementById('vi-skiptrace-modal');
		document.getElementById('vi-st-session-id').value = sessionId || '';
		document.getElementById('vi-st-results').innerHTML = '';
		['name', 'address', 'city', 'state', 'zip', 'phone', 'email'].forEach(function (f) {
			document.getElementById('vi-st-' + f).value = '';
		});
		modal.hidden = false;
	}

	function runSkipTrace() {
		var results = document.getElementById('vi-st-results');
		var seed = {
			session_id: document.getElementById('vi-st-session-id').value,
			name: document.getElementById('vi-st-name').value,
			address: document.getElementById('vi-st-address').value,
			city: document.getElementById('vi-st-city').value,
			state: document.getElementById('vi-st-state').value,
			zip: document.getElementById('vi-st-zip').value,
			phone: document.getElementById('vi-st-phone').value,
			email: document.getElementById('vi-st-email').value
		};

		results.innerHTML = '<p>Running skip trace… this can take up to a couple of minutes.</p>';

		apiFetch('/skip-trace', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(seed)
		}).then(function (data) {
			if (!data.results || !data.results.length) {
				results.innerHTML = '<p>No matches found.</p>';
				return;
			}
			results.innerHTML = data.results.map(function (r) {
				return '<pre class="vi-st-result">' + escapeHtml(JSON.stringify(r, null, 2)) + '</pre>';
			}).join('');
			loadSessions();
		}).catch(function (err) {
			results.innerHTML = '<p class="vi-error">' + escapeHtml(err.message) + '</p>';
		});
	}

	function exportReport(format) {
		var query = filterQuery();
		query.set('action', 'vi_export');
		query.set('format', format);
		query.set('_wpnonce', VI_ADMIN.exportNonce);
		window.location.href = VI_ADMIN.exportUrl + '?' + query.toString();
	}

	document.addEventListener('DOMContentLoaded', function () {
		loadStats();
		loadSessions();

		var applyBtn = document.getElementById('vi-filter-apply');
		if (applyBtn) {
			applyBtn.addEventListener('click', function () {
				state.search = document.getElementById('vi-search').value;
				state.dateFrom = document.getElementById('vi-date-from').value;
				state.dateTo = document.getElementById('vi-date-to').value;
				state.source = document.getElementById('vi-source-filter').value;
				state.page = 1;
				loadStats();
				loadSessions();
			});
		}

		var csvBtn = document.getElementById('vi-export-csv');
		if (csvBtn) csvBtn.addEventListener('click', function () { exportReport('csv'); });

		var pdfBtn = document.getElementById('vi-export-pdf');
		if (pdfBtn) pdfBtn.addEventListener('click', function () { exportReport('pdf'); });

		var stRun = document.getElementById('vi-st-run');
		if (stRun) stRun.addEventListener('click', runSkipTrace);

		var stClose = document.getElementById('vi-st-close');
		if (stClose) {
			stClose.addEventListener('click', function () {
				document.getElementById('vi-skiptrace-modal').hidden = true;
			});
		}
	});
})();
