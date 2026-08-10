(function () {
    'use strict';

    var ANALYTICS_ENDPOINT = 'https://script.google.com/macros/s/AKfycbzPF0VRKkJawXA5bCfiu0122ku_X76g_-zAMvSXsa5hMNnLllpFPLN85HU3VN8BrWVT/exec';
    var COUNTRY_ENDPOINT = 'https://api.country.is/';
    var ANALYTICS_PAGES = new Set([
        'home', 'upcoming', 'entrylists', 'draws', 'calendar', 'rankings',
        'roadtogs', 'history', 'fedbcup', 'tstrength'
    ]);
    var ANALYTICS_FILTER_PARAMS = {
        home: [],
        upcoming: ['q'],
        entrylists: ['t', 'prio'],
        draws: ['t', 'type', 'round'],
        calendar: ['level', 'continent', 'surface'],
        rankings: ['q', 'scope', 'date'],
        roadtogs: ['player'],
        history: [
            'page', 'mcat', 'qualy', 'player', 'surface', 'round', 'result',
            'year', 't', 'category', 'opp', 'oppcountry', 'entry', 'seed',
            'type', 'asrank', 'asmode', 'vsrank', 'vsmode'
        ],
        fedbcup: ['view', 'player'],
        tstrength: ['year', 'level', 'surface', 'region', 'draw', 'sort']
    };
    var SAFE_FILTER_VALUE_RE = /^[a-z0-9][a-z0-9,.-]*$/;
    var MAX_FILTER_VALUE_LENGTH = 160;
    var MAX_CONTENT_ID_LENGTH = 1024;
    var lastAggregateKey = '';
    var countryPromise = null;

    function analyticsSlug(value) {
        return (value == null ? '' : String(value))
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .replace(/-+/g, '-')
            .slice(0, 80);
    }

    var analyticsPlayerSlugs = new Set(
        Array.from(document.querySelectorAll('#roadtogsPlayerSelect option'))
            .map(function (option) { return analyticsSlug(option.value); })
            .filter(Boolean)
    );

    function canonicalFilterContentId(url, pageType) {
        var allowedKeys = ANALYTICS_FILTER_PARAMS[pageType] || [];
        var parts = [];
        allowedKeys.forEach(function (key) {
            var value = (url.searchParams.get(key) || '').trim().toLowerCase();
            if (!value || value.length > MAX_FILTER_VALUE_LENGTH || !SAFE_FILTER_VALUE_RE.test(value)) return;
            parts.push(key + '=' + value);
        });
        var contentId = parts.join('&');
        return contentId.length <= MAX_CONTENT_ID_LENGTH ? contentId : '';
    }

    function normalizedAggregatePage(pageOverride) {
        var url;
        try {
            url = new URL(pageOverride || location.href, location.href);
        } catch (error) {
            url = new URL(location.href);
        }

        var segments = url.pathname.split('/').filter(Boolean);
        var pageType = segments.length ? segments[segments.length - 1].toLowerCase() : 'home';
        if (pageType === 'app.html' || pageType === 'index.html') pageType = '';
        var hashTab = (url.hash || '').replace(/^#/, '').split(/[?&/]/)[0].toLowerCase();
        if (!ANALYTICS_PAGES.has(pageType) && ANALYTICS_PAGES.has(hashTab)) pageType = hashTab;
        if (!ANALYTICS_PAGES.has(pageType)) pageType = 'home';

        var contentId = canonicalFilterContentId(url, pageType);
        if (pageType === 'roadtogs') {
            var playerSlug = analyticsSlug(url.searchParams.get('player') || '');
            contentId = analyticsPlayerSlugs.has(playerSlug) ? playerSlug : '';
        }
        return { page_type: pageType, content_id: contentId };
    }

    function isLikelyAutomatedVisit() {
        var userAgent = (navigator.userAgent || '').toString();
        return navigator.webdriver === true
            || /(bot|crawler|spider|crawling|headlesschrome|lighthouse|google-inspectiontool|pagespeed)/i.test(userAgent);
    }

    function analyticsOptedOut() {
        return navigator.globalPrivacyControl === true || navigator.doNotTrack === '1';
    }

    function countryNameFromCode(value) {
        var code = (value == null ? '' : String(value)).trim().toUpperCase();
        if (!/^[A-Z]{2}$/.test(code)) return 'Unknown';
        try {
            if (typeof Intl !== 'undefined' && Intl.DisplayNames) {
                return new Intl.DisplayNames(['en'], { type: 'region' }).of(code) || code;
            }
        } catch (error) {}
        return code;
    }

    function lookupCountry() {
        var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        var timeout = controller ? setTimeout(function () { controller.abort(); }, 3500) : null;
        var options = {
            credentials: 'omit',
            cache: 'no-store',
            referrerPolicy: 'no-referrer'
        };
        if (controller) options.signal = controller.signal;

        return fetch(COUNTRY_ENDPOINT, options)
            .then(function (response) {
                if (!response.ok) throw new Error('Country lookup failed');
                return response.json();
            })
            .then(function (data) {
                if (timeout) clearTimeout(timeout);
                return countryNameFromCode(data && data.country);
            })
            .catch(function () {
                if (timeout) clearTimeout(timeout);
                return 'Unknown';
            });
    }

    function getCountry() {
        if (!countryPromise) countryPromise = lookupCountry();
        return countryPromise;
    }

    function sendAggregatePage(page) {
        getCountry().then(function (country) {
            fetch(ANALYTICS_ENDPOINT, {
                method: 'POST',
                body: JSON.stringify({
                    country: country,
                    page_type: page.page_type,
                    content_id: page.content_id
                }),
                mode: 'no-cors',
                credentials: 'omit',
                cache: 'no-store',
                referrerPolicy: 'no-referrer',
                keepalive: true
            }).catch(function () {});
        });
    }

    function trackVisit(pageOverride) {
        if (isLikelyAutomatedVisit() || analyticsOptedOut()) return;
        var page = normalizedAggregatePage(pageOverride);
        var aggregateKey = page.page_type + ':' + page.content_id;
        if (lastAggregateKey === aggregateKey) return;
        sendAggregatePage(page);
        lastAggregateKey = aggregateKey;
    }
    window.trackVisit = trackVisit;

    (function installUrlChangeVisitTracking() {
        function currentPage() {
            return location.pathname + location.search + (location.hash || '');
        }

        function trackAfterHistoryChange(previousPage) {
            var page = currentPage();
            if (page !== previousPage) trackVisit(page);
        }

        var originalPushState = history.pushState;
        var originalReplaceState = history.replaceState;

        history.pushState = function () {
            var previousPage = currentPage();
            var result = originalPushState.apply(this, arguments);
            trackAfterHistoryChange(previousPage);
            return result;
        };

        history.replaceState = function () {
            var previousPage = currentPage();
            var result = originalReplaceState.apply(this, arguments);
            trackAfterHistoryChange(previousPage);
            return result;
        };

        window.addEventListener('popstate', function () { trackVisit(currentPage()); });
        window.addEventListener('hashchange', function () { trackVisit(currentPage()); });
    }());

    trackVisit(location.pathname + location.search + (location.hash || ''));
}());
