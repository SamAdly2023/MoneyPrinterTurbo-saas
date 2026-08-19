(function () {
	'use strict';

	if (typeof VI_TRACKER === 'undefined' || !VI_TRACKER.restUrl) {
		return;
	}

	function getSessionId() {
		var key = 'vi_session_id';
		try {
			var existing = window.localStorage.getItem(key);
			if (existing) {
				return existing;
			}
			var fresh = (window.crypto && window.crypto.randomUUID)
				? window.crypto.randomUUID()
				: 'vi-' + Date.now() + '-' + Math.random().toString(36).slice(2);
			window.localStorage.setItem(key, fresh);
			return fresh;
		} catch (e) {
			// localStorage unavailable (private browsing, disabled storage) -
			// fall back to a per-pageview id so tracking still no-ops safely
			// rather than throwing.
			return 'vi-' + Date.now() + '-' + Math.random().toString(36).slice(2);
		}
	}

	function getUtmParams() {
		var params = new URLSearchParams(window.location.search);
		return {
			source: params.get('utm_source') || '',
			medium: params.get('utm_medium') || '',
			campaign: params.get('utm_campaign') || '',
			term: params.get('utm_term') || '',
			content: params.get('utm_content') || ''
		};
	}

	function track() {
		var payload = {
			session_id: getSessionId(),
			path: window.location.pathname + window.location.search,
			title: document.title,
			referrer: document.referrer || '',
			language: navigator.language || '',
			screen: {
				width: window.screen ? window.screen.width : 0,
				height: window.screen ? window.screen.height : 0
			},
			utm: getUtmParams()
		};

		try {
			fetch(VI_TRACKER.restUrl, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload),
				keepalive: true,
				credentials: 'omit'
			}).catch(function () {
				// Network hiccups shouldn't affect the visitor's experience.
			});
		} catch (e) {
			// Ignore - tracking must never break the page.
		}
	}

	if (document.readyState === 'complete' || document.readyState === 'interactive') {
		track();
	} else {
		document.addEventListener('DOMContentLoaded', track);
	}
})();
