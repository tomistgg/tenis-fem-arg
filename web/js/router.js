        (function() {
            const VALID_TABS = new Set([
                'home',
                'upcoming',
                'entrylists',
                'draws',
                'calendar',
                'rankings',
                'roadtogs',
                'history',
                'fedbcup',
                'tstrength',
                'information',
            ]);

            function normalizePath(path) {
                const raw = (path || '/').replace(/\/+/g, '/');
                let out = raw.startsWith('/') ? raw : ('/' + raw);
                out = out.replace(/\/+/g, '/');
                return out;
            }

            function getBasePath() {
                const baseEl = document.querySelector('base');
                if (!baseEl) return '/';
                try {
                    const u = new URL(baseEl.href, location.origin);
                    let p = normalizePath(u.pathname || '/');
                    if (!p.endsWith('/')) p += '/';
                    return p;
                } catch (e) {
                    return '/';
                }
            }

            const BASE_PATH = getBasePath();
            window.SITE_BASE_PATH = BASE_PATH;
            const baseEl = document.querySelector('base');
            if (baseEl) {
                // Freeze to an absolute path so relative fetch/src paths keep working
                // after history.replaceState() changes the visible pathname.
                baseEl.setAttribute('href', BASE_PATH);
            }

            function tabToPath(tabName) {
                if (!VALID_TABS.has(tabName)) return '';
                if (tabName === 'home') return BASE_PATH;
                return normalizePath(BASE_PATH + tabName + '/');
            }

            const originalSwitchTab = window.switchTab;
            if (typeof originalSwitchTab !== 'function') return;

            window.switchTab = function(tabName) {
                let tab = (tabName || '').trim().toLowerCase();
                if (!VALID_TABS.has(tab)) {
                    originalSwitchTab(tabName);
                    return;
                }
                try {
                    let desired = tabToPath(tab);
                    const current = normalizePath(location.pathname || '/');
                    const suffix = (current === desired || location.hash) ? (location.search || '') : '';
                    if (desired && current !== desired) {
                        history.replaceState(null, '', desired + suffix);
                    } else if (location.hash) {
                        history.replaceState(null, '', (desired || current) + suffix);
                    }
                } catch (e) {}
                originalSwitchTab(tab);
            };

            function tabFromHash() {
                const raw = (location.hash || '').replace(/^#/, '').trim().toLowerCase();
                if (!raw) return '';
                return VALID_TABS.has(raw) ? raw : '';
            }

            function tabFromPath() {
                const base = BASE_PATH.toLowerCase();
                const fullPath = normalizePath((location.pathname || '/').toLowerCase());
                let rel = fullPath.startsWith(base) ? fullPath.slice(base.length) : fullPath.replace(/^\/+/, '');
                rel = rel.replace(/index\.html$/, '');
                rel = rel.replace(/^\/+|\/+$/g, '');
                if (!rel) return 'home';
                return VALID_TABS.has(rel) ? rel : '';
            }

            function applyRoute() {
                const tab = tabFromHash() || tabFromPath();
                if (tab && tab !== 'home') {
                    window.switchTab(tab);
                    return;
                }
                try {
                    const saved = localStorage.getItem('lastTab');
                    if (saved && VALID_TABS.has(saved) && saved !== 'home') {
                        window.switchTab(saved);
                        return;
                    }
                } catch(e) {}
                if (tab) window.switchTab(tab);
            }

            document.addEventListener('DOMContentLoaded', applyRoute);
            window.addEventListener('hashchange', applyRoute);
            window.addEventListener('popstate', applyRoute);
        })();
        
