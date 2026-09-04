            const tournamentData = window.WTARG_DATA.tournaments;
            const _localScriptPromises = {};
            function _loadLocalScriptOnce(src) {
                if (_localScriptPromises[src]) return _localScriptPromises[src];
                _localScriptPromises[src] = new Promise((resolve, reject) => {
                    if (!src) {
                        reject(new Error('Missing script src'));
                        return;
                    }
                    const script = document.createElement('script');
                    script.src = src;
                    script.async = true;
                    script.dataset.localBundle = src;
                    script.onload = () => {
                        resolve();
                    };
                    script.onerror = () => {
                        _localScriptPromises[src] = null;
                        reject(new Error('Failed to load ' + src));
                    };
                    document.head.appendChild(script);
                });
                return _localScriptPromises[src];
            }

            const URL_STATE_TABS = new Set([
                'home', 'upcoming', 'entrylists', 'draws', 'calendar', 'rankings',
                'roadtogs', 'history', 'fedbcup', 'tstrength', 'information'
            ]);
            let _currentTabName = 'home';
            let _urlStateApplying = false;
            let _urlStateSwitching = false;
            let _urlStateRestoreSeq = 0;
            let _urlStateLastTrackRequest = '';

            function normalizeUrlPath(path) {
                let out = (path || '/').toString().replace(/\/+$/g, '/');
                out = out.startsWith('/') ? out : ('/' + out);
                out = out.replace(/\/+/g, '/');
                return out;
            }

            function getUrlBasePath() {
                if (window.SITE_BASE_PATH) return window.SITE_BASE_PATH;
                const baseEl = document.querySelector('base');
                if (!baseEl) return '/';
                try {
                    const u = new URL(baseEl.href, location.origin);
                    let p = normalizeUrlPath(u.pathname || '/');
                    if (!p.endsWith('/')) p += '/';
                    return p;
                } catch (e) {
                    return '/';
                }
            }

            function tabPathForUrlState(tabName) {
                const base = getUrlBasePath();
                const tab = (tabName || 'home').toString().trim().toLowerCase();
                if (!URL_STATE_TABS.has(tab) || tab === 'home') return base;
                return normalizeUrlPath(base + tab + '/');
            }

            function trackedFullUrl() {
                return location.pathname + location.search + (location.hash || '');
            }

            function slugStateValue(value) {
                const raw = (value == null ? '' : String(value)).trim();
                if (!raw) return '';
                return raw
                    .normalize('NFD')
                    .replace(/[\u0300-\u036f]/g, '')
                    .toLowerCase()
                    .replace(/&/g, ' and ')
                    .replace(/[^a-z0-9]+/g, '-')
                    .replace(/^-+|-+$/g, '')
                    .replace(/-+/g, '-');
            }

            function deslugSearchValue(value) {
                return (value || '').toString().trim().replace(/-/g, ' ');
            }

            function stateSlugMatches(value, slug) {
                const wanted = slugStateValue(slug);
                if (!wanted) return false;
                const raw = (value == null ? '' : String(value)).trim().toLowerCase();
                return raw === wanted || slugStateValue(value) === wanted;
            }

            function entryTournamentStateSlugFromKey(key) {
                const raw = (key == null ? '' : String(key)).trim();
                if (!raw) return '';
                const wta = raw.match(/\/tournaments\/([^\/?#]+)\/([^\/?#]+)\/([^\/?#]+)\/player-list/i);
                if (wta) return slugStateValue(wta[1] + '-' + wta[2] + '-' + wta[3]);
                return slugStateValue(raw);
            }

            function readUrlParams() {
                try {
                    return new URLSearchParams(location.search || '');
                } catch (e) {
                    return new URLSearchParams();
                }
            }

            function splitUrlStateList(value) {
                const raw = (value || '').toString().trim();
                if (!raw) return [];
                if (raw === 'none') return [];
                return raw.split(',').map(part => slugStateValue(part)).filter(Boolean);
            }

            function setStateParam(params, key, value, options = {}) {
                if (value == null) return;
                if (Array.isArray(value)) {
                    const items = value.map(v => slugStateValue(v)).filter(Boolean);
                    if (!items.length) return;
                    params.set(key, items.join(','));
                    return;
                }
                const raw = String(value).trim();
                if (!raw) return;
                params.set(key, options.raw ? raw : slugStateValue(raw));
            }

            function writeUrlStateForTab(tabName, state, options = {}) {
                const tab = (tabName || _currentTabName || 'home').toString().trim().toLowerCase();
                if (!URL_STATE_TABS.has(tab)) return;
                const params = new URLSearchParams();
                Object.keys(state || {}).forEach(key => {
                    const value = state[key];
                    const raw = key === 'q' || key === 'date' || key === 'asrank' || key === 'vsrank' || key === 'type' || key === 'draw';
                    setStateParam(params, key, value, { raw });
                });
                const query = params.toString().replace(/%2C/g, ',');
                const querySuffix = query ? ('?' + query) : '';
                const isLocalFile = location.protocol === 'file:';
                // A file:// page may only replace history with the same physical
                // file. Pointing it at /rankings/ raises a SecurityError after the
                // rankings have rendered, which previously looked like a data-load
                // failure. Keep local state on app.html and use the tab hash.
                const target = isLocalFile
                    ? location.href.split(/[?#]/)[0] + querySuffix + '#' + tab
                    : tabPathForUrlState(tab) + querySuffix;
                const current = isLocalFile
                    ? location.href
                    : location.pathname + location.search;
                if (current !== target) {
                    try {
                        history.replaceState(null, '', target);
                    } catch (err) {
                        // URL synchronization is optional and must never break data rendering.
                        console.warn('URL state update skipped:', err);
                    }
                }
                if (options.track !== false) trackCurrentUrlState();
            }

            function trackCurrentUrlState() {
                if (_urlStateApplying) return;
                const full = trackedFullUrl();
                if (_urlStateLastTrackRequest === full) return;
                _urlStateLastTrackRequest = full;
                if (window.trackVisit) window.trackVisit(full);
            }

            function syncUrlStateForTab(tabName, options = {}) {
                const tab = (tabName || _currentTabName || 'home').toString().trim().toLowerCase();
                if (_urlStateApplying || _urlStateSwitching) return;
                if (tab !== _currentTabName) return;
                writeUrlStateForTab(tab, collectUrlStateForTab(tab), options);
            }
            window.syncUrlStateForTab = syncUrlStateForTab;

            function restoreAndSyncUrlStateForTab(tabName) {
                const tab = (tabName || _currentTabName || 'home').toString().trim().toLowerCase();
                const seq = ++_urlStateRestoreSeq;
                Promise.resolve(restoreUrlStateForTab(tab))
                    .catch(err => console.warn('URL filter restore skipped:', err))
                    .finally(() => {
                        if (seq === _urlStateRestoreSeq) syncUrlStateForTab(tab, { track: true });
                    });
            }

            function findSelectValueBySlug(select, slug) {
                if (!select || !slug) return '';
                const wanted = slugStateValue(slug);
                const options = Array.from(select.options || []);
                const exact = options.find(opt => (opt.value || '').toString().trim().toLowerCase() === wanted);
                if (exact) return exact.value;
                const byValue = options.find(opt => stateSlugMatches(opt.value, wanted));
                if (byValue) return byValue.value;
                const byText = options.find(opt => stateSlugMatches(opt.textContent, wanted));
                return byText ? byText.value : '';
            }

            function setSelectValueFromSlug(select, slug) {
                const value = findSelectValueBySlug(select, slug);
                if (!value && slugStateValue(slug)) return false;
                select.value = value;
                if (window.jQuery && $(select).data('select2')) {
                    $(select).val(value).trigger('change.select2');
                }
                return true;
            }
            window.setSelectValueFromSlug = setSelectValueFromSlug;

            function getSelectedCheckboxValues(selector, attrName) {
                return Array.from(document.querySelectorAll(selector))
                    .filter(cb => cb.checked)
                    .map(cb => cb.getAttribute(attrName) || '')
                    .filter(Boolean);
            }

            function collectCheckboxGroupState(selector, attrName) {
                const toggles = Array.from(document.querySelectorAll(selector));
                if (!toggles.length) return null;
                const selected = toggles
                    .filter(cb => cb.checked)
                    .map(cb => cb.getAttribute(attrName) || '')
                    .filter(Boolean);
                if (selected.length === toggles.length) return null;
                return selected.length ? selected : 'none';
            }

            function restoreCheckboxGroupState(params, key, selector, attrName) {
                if (!params.has(key)) return;
                const raw = params.get(key) || '';
                const selected = new Set(splitUrlStateList(raw));
                document.querySelectorAll(selector).forEach(cb => {
                    const value = cb.getAttribute(attrName) || '';
                    cb.checked = raw === 'none' ? false : selected.has(slugStateValue(value));
                });
            }

            function collectUpcomingUrlState() {
                const input = document.getElementById('s');
                return input && input.value.trim() ? { q: slugStateValue(input.value) } : {};
            }

            function restoreUpcomingUrlState(params) {
                if (!params.has('q')) return;
                const input = document.getElementById('s');
                if (!input) return;
                input.value = deslugSearchValue(params.get('q'));
                filter();
            }

            function collectEntryListUrlState() {
                const active = document.querySelector('#view-entrylists .entry-menu-item.active');
                const state = {};
                if (active) state.t = entryTournamentStateSlugFromKey(active.getAttribute('data-key')) || entryMenuNameForItem(active);
                if (_prioFilterActive) state.prio = '1';
                return state;
            }

            function restoreEntryListUrlState(params) {
                const tSlug = params.get('t');
                if (tSlug) {
                    const item = Array.from(document.querySelectorAll('#view-entrylists .entry-menu-item')).find(el => {
                        const key = el.getAttribute('data-key') || '';
                        return stateSlugMatches(entryTournamentStateSlugFromKey(key), tSlug)
                            || stateSlugMatches(key, tSlug)
                            || stateSlugMatches(entryMenuNameForItem(el), tSlug);
                    });
                    if (item) selectEntryTournament(item);
                }
                if (params.has('prio')) {
                    _prioFilterActive = ['1', 'true', 'yes'].includes((params.get('prio') || '').toLowerCase());
                    updateEntryList();
                }
            }

            function collectCalendarUrlState() {
                const state = {};
                const levels = collectCheckboxGroupState('[data-cal-filter-toggle]', 'data-cal-filter-toggle');
                const continents = collectCheckboxGroupState('[data-cal-continent-toggle]', 'data-cal-continent-toggle');
                const surfaces = collectCheckboxGroupState('[data-cal-surface-toggle]', 'data-cal-surface-toggle');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (levels !== null) state.level = levels;
                if (continents !== null) state.continent = continents;
                if (surfaces !== null) state.surface = surfaces;
                if (gmToggle && gmToggle.getAttribute('aria-pressed') === 'false') state.gm = '0';
                return state;
            }

            function restoreCalendarUrlState(params) {
                restoreCheckboxGroupState(params, 'level', '[data-cal-filter-toggle]', 'data-cal-filter-toggle');
                restoreCheckboxGroupState(params, 'continent', '[data-cal-continent-toggle]', 'data-cal-continent-toggle');
                restoreCheckboxGroupState(params, 'surface', '[data-cal-surface-toggle]', 'data-cal-surface-toggle');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (gmToggle && params.has('gm')) {
                    const showGm = !['0', 'false', 'no', 'off'].includes((params.get('gm') || '').toLowerCase());
                    gmToggle.setAttribute('aria-pressed', showGm ? 'true' : 'false');
                }
                applyCalendarFilters();
            }

            function collectRankingsUrlState() {
                const state = {};
                const search = document.getElementById('rankings-search');
                if (search && search.value.trim()) state.q = slugStateValue(search.value);
                if (showArgOnly) state.scope = 'arg';
                const year = document.getElementById('rankings-year-select');
                const month = document.getElementById('rankings-month-select');
                const day = document.getElementById('rankings-day-select');
                if (year && month && day && year.value && month.value && day.value) {
                    state.date = year.value + '-' + String(month.value).padStart(2, '0') + '-' + String(day.value).padStart(2, '0');
                }
                return state;
            }

            function restoreRankingsUrlState(params) {
                const search = document.getElementById('rankings-search');
                if (search && params.has('q')) search.value = deslugSearchValue(params.get('q'));
                if (params.has('scope')) {
                    showArgOnly = (params.get('scope') || '').toLowerCase() === 'arg';
                    const btn = document.getElementById('rankings-toggle-btn');
                    const view = document.getElementById('view-rankings');
                    if (btn) btn.innerHTML = showArgOnly ? 'Show ALL' : 'Show <img class="btn-flag-icon" src="assets/argentina.png" alt="ARG">';
                    if (view) view.classList.toggle('rankings-show-all', !showArgOnly);
                }
                const date = params.get('date') || '';
                if (date.length === 10) {
                    const parts = date.split('-');
                    const year = parts[0], month = parseInt(parts[1], 10), day = parseInt(parts[2], 10);
                    if (_rankingsDatesIndex[year] && _rankingsDatesIndex[year][String(month)] && _rankingsDatesIndex[year][String(month)].includes(day)) {
                        const ySel = document.getElementById('rankings-year-select');
                        if (ySel) ySel.value = year;
                        _populateRankingMonths(year, month, day);
                        return switchRankingWeek(date);
                    }
                }
                filterRankings();
            }

            function collectRoadToGSUrlState() {
                const selectedPlayer = getNormalizedPlayerSelection('roadtogsPlayerSelect');
                const state = selectedPlayer ? { player: selectedPlayer } : {};
                const cutoffSelect = document.getElementById('roadtogs-cutoff-select');
                if (cutoffSelect && cutoffSelect.value && cutoffSelect.value !== 'live') {
                    state.cutoff = cutoffSelect.value;
                }
                return state;
            }

            function restoreRoadToGSUrlState(params) {
                const cutoffSelect = document.getElementById('roadtogs-cutoff-select');
                const cutoff = params.get('cutoff') || 'live';
                if (cutoffSelect) setSelectValueFromSlug(cutoffSelect, cutoff);
                if (!params.has('player')) return;
                const select = document.getElementById('roadtogsPlayerSelect');
                if (!select || !setSelectValueFromSlug(select, params.get('player'))) return;
                return renderRoadToGS();
            }

            function collectFedBjkUrlState() {
                const state = {};
                const activeBtn = document.querySelector('#view-fedbcup .fedbcup-btn.active');
                if (activeBtn && activeBtn.id) state.view = activeBtn.id.replace('fedbcup-btn-', '');
                const select = document.getElementById('fedbcup-player-filter');
                if (select && select.value) state.player = select.value;
                return state;
            }

            function restoreFedBjkUrlState(params) {
                const view = slugStateValue(params.get('view') || '');
                if (['series', 'players', 'captains'].includes(view)) switchFedBjkTab(view);
                const select = document.getElementById('fedbcup-player-filter');
                if (select && params.has('player') && setSelectValueFromSlug(select, params.get('player'))) {
                    filterFedBjkPlayer();
                }
            }

            const HISTORY_URL_MULTI_FILTERS = [
                ['surface', 'filter-surface'],
                ['round', 'filter-round'],
                ['result', 'filter-result'],
                ['year', 'filter-year'],
                ['category', 'filter-category'],
                ['oppcountry', 'filter-opponent-country'],
                ['entry', 'filter-player-entry'],
                ['seed', 'filter-seed'],
                ['type', 'filter-match-type']
            ];

            function applyFilterOptionSlugs(filterId, slugs) {
                const container = document.getElementById(filterId);
                if (!container || !slugs.length) return false;
                const selected = new Set(slugs.map(slugStateValue).filter(Boolean));
                let matched = false;
                container.querySelectorAll('.filter-option').forEach(option => {
                    const value = option.getAttribute('data-value') || option.textContent || '';
                    const isSelected = selected.has(slugStateValue(value));
                    option.classList.toggle('selected', isSelected);
                    option.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
                    if (isSelected) matched = true;
                });
                return matched;
            }

            function collectHistoryUrlState() {
                const state = {};
                const page = typeof historySubpage === 'string' ? historySubpage : 'match';
                if (page && page !== 'match') state.page = page;
                if (page === 'milestones') {
                    if (typeof getMilestonesFilterState === 'function' && typeof getMilestonesCategoryDefs === 'function') {
                        const defs = getMilestonesCategoryDefs();
                        const selected = getMilestonesFilterState();
                        if (defs.length && selected.categories.length !== defs.length) state.mcat = selected.categories;
                        if (!selected.includeQualy) state.qualy = '0';
                    }
                    return state;
                }
                const player = getNormalizedPlayerSelection('playerHistorySelect');
                if (player) state.player = player === '__ALL__' ? 'all' : player;
                if (!player) return state;
                const filters = getHistoryFilterSelectionState();
                if (filters.surfaces.length) state.surface = filters.surfaces;
                if (filters.rounds.length) state.round = filters.rounds;
                if (filters.results.length) state.result = filters.results;
                if (filters.years.length) state.year = filters.years;
                if (filters.tournament) state.t = filters.tournament;
                if (filters.categories.length) state.category = filters.categories;
                if (filters.opponent) state.opp = filters.opponent;
                if (filters.opponentCountries.length) state.oppcountry = filters.opponentCountries;
                if (filters.playerEntries.length) state.entry = filters.playerEntries;
                if (filters.seeds.length) state.seed = filters.seeds;
                if (filters.matchTypes.length) state.type = filters.matchTypes;
                if (filters.asRankVal !== null) {
                    state.asrank = String(filters.asRankVal);
                    if (filters.asRankMode !== 'higher') state.asmode = filters.asRankMode;
                }
                if (filters.vsRankVal !== null) {
                    state.vsrank = String(filters.vsRankVal);
                    if (filters.vsRankMode !== 'higher') state.vsmode = filters.vsRankMode;
                }
                return state;
            }

            async function restoreHistoryUrlState(params) {
                const page = slugStateValue(params.get('page') || '');
                if (page === 'milestones') {
                    setHistorySubpage('milestones');
                    await renderMilestonesPage();
                    restoreMilestonesUrlState(params);
                    await renderMilestonesPage();
                    return;
                }

                setHistorySubpage('match');
                const playerSlug = params.get('player');
                const select = document.getElementById('playerHistorySelect');
                if (playerSlug && select) {
                    let value = playerSlug.toLowerCase() === 'all' ? '__ALL__' : findSelectValueBySlug(select, playerSlug);
                    if (value) {
                        select.value = value;
                        if (window.jQuery && $(select).data('select2')) $(select).val(value).trigger('change.select2');
                        await filterHistoryByPlayer();
                    }
                }
                if (!getNormalizedPlayerSelection('playerHistorySelect')) return;

                HISTORY_URL_MULTI_FILTERS.forEach(([paramName, filterId]) => {
                    applyFilterOptionSlugs(filterId, splitUrlStateList(params.get(paramName)));
                });

                const tournSelect = document.getElementById('filter-tournament-select');
                const tournSlug = params.get('t') || params.get('tournament');
                if (tournSelect && tournSlug) setSelectValueFromSlug(tournSelect, tournSlug);

                const oppSelect = document.getElementById('filter-opponent-select');
                if (oppSelect && params.has('opp')) setSelectValueFromSlug(oppSelect, params.get('opp'));

                const asRankInput = document.getElementById('filter-as-rank');
                const asRankMode = document.getElementById('filter-as-rank-mode');
                const vsRankInput = document.getElementById('filter-vs-rank');
                const vsRankMode = document.getElementById('filter-vs-rank-mode');
                if (asRankInput && params.has('asrank')) asRankInput.value = (params.get('asrank') || '').replace(/\D/g, '');
                if (vsRankInput && params.has('vsrank')) vsRankInput.value = (params.get('vsrank') || '').replace(/\D/g, '');
                if (asRankMode && ['higher', 'lower'].includes(params.get('asmode'))) asRankMode.value = params.get('asmode');
                if (vsRankMode && ['higher', 'lower'].includes(params.get('vsmode'))) vsRankMode.value = params.get('vsmode');
                applyHistoryFilters();
            }

            function restoreMilestonesUrlState(params) {
                const selectedCats = splitUrlStateList(params.get('mcat'));
                if (selectedCats.length) {
                    getMilestonesCategoryDefs().forEach(def => {
                        const el = document.getElementById(def.id);
                        if (el) el.checked = selectedCats.includes(slugStateValue(def.key));
                    });
                }
                const qualy = document.getElementById('milestones-filter-qualy');
                if (qualy && params.has('qualy')) qualy.checked = params.get('qualy') !== '0';
            }

            function collectDrawsUrlState() {
                const state = {};
                const select = document.getElementById('draws-tournament-select');
                const key = currentDrawTKey || (select ? select.value : '');
                if (key) state.t = entryTournamentStateSlugFromKey(key) || key;
                if (currentDrawType) state.type = currentDrawType;
                if (currentDrawFilterRound > 0) state.round = String(currentDrawFilterRound + 1);
                return state;
            }

            function restoreDrawsUrlState(params) {
                const select = document.getElementById('draws-tournament-select');
                let key = params.has('t') && select ? findSelectValueBySlug(select, params.get('t')) : '';
                if (!key && select && select.value) key = select.value;
                const requestedType = (params.get('type') || '').toUpperCase();
                if (requestedType) currentDrawType = requestedType;
                if (key) {
                    if (select) select.value = key;
                    onDrawTournamentChange(key);
                }
                const round = parseInt(params.get('round') || '', 10);
                if (Number.isInteger(round) && round > 1) filterDrawFromRound(round - 1);
            }

            function collectTStrengthUrlState() {
                const state = {};
                const year = document.getElementById('ts-filter-year');
                const level = document.getElementById('ts-filter-level');
                const surface = document.getElementById('ts-filter-surface');
                const region = document.getElementById('ts-filter-region');
                if (year && year.value) state.year = year.value;
                if (level && level.value) state.level = level.value;
                if (surface && surface.value) state.surface = surface.value;
                if (region && region.value) state.region = region.value;
                if (window.__wtargTStrengthView && window.__wtargTStrengthView !== 'MD') state.draw = window.__wtargTStrengthView;
                if (window.__wtargTStrengthSort && window.__wtargTStrengthSort !== 'date') state.sort = window.__wtargTStrengthSort;
                return state;
            }

            function restoreTStrengthUrlState(params) {
                if (window.__restoreTStrengthState) window.__restoreTStrengthState(params);
            }

            function collectUrlStateForTab(tabName) {
                switch (tabName) {
                    case 'upcoming': return collectUpcomingUrlState();
                    case 'entrylists': return collectEntryListUrlState();
                    case 'draws': return collectDrawsUrlState();
                    case 'calendar': return collectCalendarUrlState();
                    case 'rankings': return collectRankingsUrlState();
                    case 'roadtogs': return collectRoadToGSUrlState();
                    case 'history': return collectHistoryUrlState();
                    case 'fedbcup': return collectFedBjkUrlState();
                    case 'tstrength': return collectTStrengthUrlState();
                    default: return {};
                }
            }

            async function restoreUrlStateForTab(tabName) {
                const params = readUrlParams();
                _urlStateApplying = true;
                try {
                    switch (tabName) {
                        case 'upcoming': restoreUpcomingUrlState(params); break;
                        case 'entrylists': restoreEntryListUrlState(params); break;
                        case 'draws': restoreDrawsUrlState(params); break;
                        case 'calendar': restoreCalendarUrlState(params); break;
                        case 'rankings': await restoreRankingsUrlState(params); break;
                        case 'roadtogs': await restoreRoadToGSUrlState(params); break;
                        case 'history': await restoreHistoryUrlState(params); break;
                        case 'fedbcup': restoreFedBjkUrlState(params); break;
                        case 'tstrength': restoreTStrengthUrlState(params); break;
                    }
                } finally {
                    _urlStateApplying = false;
                }
            }
            const playerMapping = window.__WTA_PLAYER_MAPPING__ || {};

            function normalizeHistoryPlayerName(rawName, source, playerId) {
                const name = (rawName || '').toString().trim();
                if (!name) return name;
                if (name.includes('/')) {
                    return name.split('/').map(part => {
                        const trimmed = part.trim();
                        return trimmed ? normalizeHistoryPlayerName(trimmed, source, '') : trimmed;
                    }).join(' / ');
                }
                const mapped = getDisplayNameForIdentity(source, playerId, name);
                return (mapped || name).toString();
            }

            function normalizeHistoryRow(row) {
                if (!row || typeof row !== 'object' || Array.isArray(row)) return row;
                const normalized = { ...row };
                const source = historyIdentitySource(normalized);
                [
                    '_winnerName', '_loserName', 'winnerName', 'loserName',
                    'winner_name', 'loser_name', 'PLAYER', 'OPPONENT', 'RIVAL',
                    'player', 'opponent', 'rival'
                ].forEach(field => {
                    const value = normalized[field];
                    if (typeof value === 'string' && value.trim() && !value.includes('/')) {
                        const fieldLower = field.toLowerCase();
                        const side = fieldLower.includes('winner')
                            ? 'winner'
                            : fieldLower.includes('loser') ? 'loser' : '';
                        const playerId = side ? normalized[`_${side}Id`] : '';
                        normalized[field] = normalizeHistoryPlayerName(value, source, playerId);
                    }
                });
                return normalized;
            }

            function expandHistoryData(rows) {
                if (!Array.isArray(rows) || !rows.length) return rows;
                const first = rows[0];
                if (!first || typeof first !== 'object' || Array.isArray(first) || !Array.isArray(first.rows)) return rows;
                const expanded = [];
                rows.forEach(group => {
                    if (!group || typeof group !== 'object' || Array.isArray(group) || !Array.isArray(group.rows)) return;
                    const shared = {};
                    Object.keys(group).forEach(key => {
                        if (key !== 'rows') shared[key] = group[key];
                    });
                    group.rows.forEach(row => {
                        if (!row || typeof row !== 'object' || Array.isArray(row)) return;
                        const merged = { ...shared, ...row };
                        [
                            'PLAYER', 'ENTRY', 'SEED', 'RESULT',
                            'RIVAL_ENTRY', 'RIVAL_SEED', 'RIVAL', 'RIVAL_COUNTRY'
                        ].forEach(field => {
                            if (!(field in merged)) merged[field] = '';
                        });
                        expanded.push(merged);
                    });
                });
                return expanded;
            }

            function normalizeHistoryData(rows) {
                const expanded = expandHistoryData(rows);
                return Array.isArray(expanded) ? expanded.map(normalizeHistoryRow) : expanded;
            }

            let historyData = window.__WTA_HISTORY_DATA__ || null;
            let _historyDataNormalized = false;
            let _historyDataPromise = null;
            function ensureHistoryDataLoaded() {
                if (Array.isArray(historyData)) {
                    if (!_historyDataNormalized) {
                        historyData = normalizeHistoryData(historyData);
                        window.__WTA_HISTORY_DATA__ = historyData;
                        _historyDataNormalized = true;
                    }
                    return Promise.resolve(historyData);
                }
                if (window.__WTA_HISTORY_DATA__ && Array.isArray(window.__WTA_HISTORY_DATA__)) {
                    historyData = normalizeHistoryData(window.__WTA_HISTORY_DATA__);
                    window.__WTA_HISTORY_DATA__ = historyData;
                    _historyDataNormalized = true;
                    return Promise.resolve(historyData);
                }
                if (_historyDataPromise) return _historyDataPromise;
                _historyDataPromise = _loadLocalScriptOnce('data/history_data_bundle.js')
                    .then(() => {
                        const d = window.__WTA_HISTORY_DATA__;
                        if (!Array.isArray(d)) throw new Error('History bundle did not initialize');
                        historyData = normalizeHistoryData(d);
                        window.__WTA_HISTORY_DATA__ = historyData;
                        _historyDataNormalized = true;
                        _historyDataPromise = null;
                        return historyData;
                    })
                    .catch(err => {
                        _historyDataPromise = null;
                        throw err;
                    });
                return _historyDataPromise;
            }
            const pointsDistribution = window.WTARG_DATA.pointsDistribution;
            const itfDrawSizes = window.WTARG_DATA.itfDrawSizes;
            const wtaDrawSizes = window.WTARG_DATA.wtaDrawSizes;
            const historicalDrawSlots = window.WTARG_DATA.historicalDrawSlots;
            const gsCutoffs = window.WTARG_DATA.gsCutoffs;
            const drawsData = window.WTARG_DATA.draws;
            const drawsTournamentInfo = window.WTARG_DATA.drawsTournamentInfo;
            const _iocToIso2 = {ALB:'al',ALG:'dz',AND:'ad',ANG:'ao',ARG:'ar',ARM:'am',ASA:'as',AUS:'au',AUT:'at',AZE:'az',BAH:'bs',BAR:'bb',BDI:'bi',BEL:'be',BEN:'bj',BIH:'ba',BLR:'by',BOL:'bo',BOT:'bw',BRA:'br',BUL:'bg',CAL:'nc',CAM:'kh',CAN:'ca',CHE:'ch',CHI:'cl',CHL:'cl',CHN:'cn',CIV:'ci',CMR:'cm',COD:'cd',COL:'co',CRC:'cr',CRO:'hr',CUB:'cu',CUW:'cw',CYP:'cy',CZE:'cz',CZS:'cz',DEN:'dk',DOM:'do',DZA:'dz',ECU:'ec',EGY:'eg',ERI:'er',ESA:'sv',ESP:'es',EST:'ee',FIJ:'fj',FIN:'fi',FRA:'fr',FRG:'de',GAB:'ga',GBR:'gb',GEO:'ge',GER:'de',GHA:'gh',GLP:'gp',GRB:'gb',GRE:'gr',GRC:'gr',GUA:'gt',HAI:'ht',HKG:'hk',HRV:'hr',HUN:'hu',INA:'id',IND:'in',IRI:'ir',IRL:'ie',IRN:'ir',ISR:'il',ITA:'it',JAM:'jm',JOR:'jo',JPN:'jp',KAZ:'kz',KEN:'ke',KGZ:'kg',KHM:'kh',KOR:'kr',KOS:'xk',KSA:'sa',LAO:'la',LAT:'lv',LIE:'li',LTU:'lt',LUX:'lu',MAD:'mg',MAR:'ma',MAS:'my',MDA:'md',MEX:'mx',MKD:'mk',MLI:'ml',MLT:'mt',MNE:'me',MON:'mc',MRI:'mu',MOZ:'mz',NAM:'na',NCA:'ni',NCD:'nc',NED:'nl',NEP:'np',NET:'nl',NGA:'ng',NGR:'ng',NOR:'no',NZL:'nz',OMA:'om',OMN:'om',PAK:'pk',PAN:'pa',PAR:'py',PER:'pe',PHI:'ph',PLE:'ps',PNG:'pg',POL:'pl',POR:'pt',PUR:'pr',QAT:'qa',ROC:'ru',ROM:'ro',ROU:'ro',RSA:'za',RUS:'ru',SAF:'za',SAM:'ws',SEN:'sn',SGP:'sg',SIN:'sg',SLO:'si',SMR:'sm',SRB:'rs',SRI:'lk',SUI:'ch',SVK:'sk',SWE:'se',SYR:'sy',TCH:'cz',THA:'th',TKM:'tm',TOG:'tg',TPE:'tw',TRI:'tt',TTO:'tt',TUN:'tn',TUR:'tr',UAE:'ae',UKR:'ua',URU:'uy',USA:'us',UZB:'uz',VEN:'ve',VIE:'vn',XKX:'xk',ZAM:'zm',ZIM:'zw'};
            const _localFlags = new Set(['AHO','YUG','SCG','CIS','URS']);
            function countryFlag(code, showCode) {
                if (!code || code === '-') return code || '';
                const upper = code.toUpperCase();
                if (_localFlags.has(upper)) {
                    const img = `<img src="data/flags/${upper.toLowerCase()}.svg" alt="${code}" title="${code}" style="vertical-align:middle;margin-right:3px;width:16px;height:11px;outline:0.3px solid #000">`;
                    return showCode === false ? img : img + code;
                }
                const iso = _iocToIso2[upper];
                if (!iso) return code;
                const img = `<img src="https://purecatamphetamine.github.io/country-flag-icons/3x2/${iso.toUpperCase()}.svg" alt="${code}" title="${code}" style="vertical-align:middle;margin-right:3px;width:16px;height:11px;outline:0.3px solid #000">`;
                return showCode === false ? img : img + code;
            }
            function countryFlagHistory(code, showCode) {
                const html = countryFlag(code, showCode);
                if (window.innerWidth > 768) return html;
                return String(html).replace('width:16px;height:11px', 'width:12px;height:8px');
            }
            // Icon swapping is CSS-driven via [data-theme="dark"]; JS only
            // manages the data-theme attribute, localStorage, and the label.
            function _syncHomeDarkBtn(isDark) {
                const lbl = document.getElementById('home-dark-label');
                if (lbl) lbl.textContent = isDark ? 'Light Mode' : 'Dark Mode';
            }
            function toggleDarkMode() {
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                if (isDark) {
                    document.documentElement.removeAttribute('data-theme');
                    localStorage.setItem('theme', 'light');
                } else {
                    document.documentElement.setAttribute('data-theme', 'dark');
                    localStorage.setItem('theme', 'dark');
                }
                _syncHomeDarkBtn(!isDark);
            }
            _syncHomeDarkBtn(document.documentElement.getAttribute('data-theme') === 'dark');

            function toggleMobileMenu() {
                const sidebar = document.getElementById('sidebar');
                sidebar.classList.toggle('mobile-hidden');
            }

            // Close mobile menu when clicking outside
            document.addEventListener('click', function(event) {
                const sidebar = document.getElementById('sidebar');
                const menuToggle = document.querySelector('.mobile-menu-toggle');

                if (window.innerWidth <= 768) {
                    if (!sidebar.contains(event.target) && !menuToggle.contains(event.target)) {
                        sidebar.classList.add('mobile-hidden');
                    }
                }
            });

            // Close mobile menu when tab is clicked
            let homeLocked = false;
            let calendarFiltersInitialized = false;
            function closeAllCalendarDropdowns() {
                const toolbar = document.getElementById('calendar-toolbar');
                if (!toolbar) return;
                toolbar.querySelectorAll('.cal-dd.open').forEach(dd => {
                    dd.classList.remove('open');
                    const btn = dd.querySelector('[data-cal-dd-btn]');
                    if (btn) btn.setAttribute('aria-expanded', 'false');
                });
            }
            function initCalendarDropdowns() {
                const toolbar = document.getElementById('calendar-toolbar');
                if (!toolbar) return;
                if (toolbar.dataset.calDdInit === '1') return;
                toolbar.dataset.calDdInit = '1';

                toolbar.addEventListener('click', function(e) {
                    const btn = e.target.closest('[data-cal-dd-btn]');
                    if (!btn) return;
                    const dd = btn.closest('.cal-dd');
                    if (!dd) return;
                    const wasOpen = dd.classList.contains('open');
                    closeAllCalendarDropdowns();
                    if (!wasOpen) {
                        dd.classList.add('open');
                        btn.setAttribute('aria-expanded', 'true');
                    }
                    e.preventDefault();
                });

                if (!window.__calendarDdDocInit) {
                    window.__calendarDdDocInit = true;
                    document.addEventListener('click', function(e) {
                        const tb = document.getElementById('calendar-toolbar');
                        if (!tb) return;
                        if (!tb.contains(e.target)) closeAllCalendarDropdowns();
                    });
                    document.addEventListener('keydown', function(e) {
                        if (e.key === 'Escape') closeAllCalendarDropdowns();
                    });
                }
            }
            function initCalendarHorizontalScroll() {
                const view = document.getElementById('view-calendar');
                if (!view) return;
                if (view.dataset.calHScrollInit === '1') return;
                const wrapper = view.querySelector('.table-wrapper');
                if (!wrapper) return;
                view.dataset.calHScrollInit = '1';

                function hasHorizontalOverflow() {
                    return (wrapper.scrollWidth - wrapper.clientWidth) > 2;
                }

                view.addEventListener('wheel', function(e) {
                    if (e.ctrlKey) return;
                    if (e.target && e.target.closest && e.target.closest('.cal-dd-panel')) return;
                    let delta = 0;
                    if (e.deltaX && Math.abs(e.deltaX) > 0) delta = e.deltaX;
                    else if (e.shiftKey && e.deltaY && Math.abs(e.deltaY) > 0) delta = e.deltaY;
                    if (!delta) return;
                    if (!hasHorizontalOverflow()) return;
                    wrapper.scrollLeft += delta;
                    e.preventDefault();
                }, { passive: false });

                wrapper.addEventListener('wheel', function(e) {
                    if (e.ctrlKey) return;
                    if (e.shiftKey) return;
                    if (e.target && e.target.closest && e.target.closest('.cal-dd-panel')) return;
                    if (!hasHorizontalOverflow()) return;
                    if (!e.deltaY || Math.abs(e.deltaY) < 1) return;
                    if (e.deltaX && Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
                    const before = wrapper.scrollLeft;
                    wrapper.scrollLeft += e.deltaY;
                    if (wrapper.scrollLeft !== before) e.preventDefault();
                }, { passive: false });

                let dragging = false;
                let dragStartX = 0;
                let dragStartScrollLeft = 0;
                wrapper.addEventListener('mousedown', function(e) {
                    if (e.button !== 0) return;
                    if (!hasHorizontalOverflow()) return;
                    dragging = true;
                    wrapper.classList.add('dragging');
                    dragStartX = e.pageX;
                    dragStartScrollLeft = wrapper.scrollLeft;
                });
                window.addEventListener('mouseup', function() {
                    dragging = false;
                    wrapper.classList.remove('dragging');
                });
                window.addEventListener('mousemove', function(e) {
                    if (!dragging) return;
                    const dx = e.pageX - dragStartX;
                    wrapper.scrollLeft = dragStartScrollLeft - dx;
                });

            }
            function syncCalendarRowspans() {
                const table = document.querySelector('#view-calendar .calendar-table');
                if (!table) return;
                const rows = Array.from(table.querySelectorAll('tbody tr'));
                if (!rows.length) return;

                const groupFirstRows = Array.from(table.querySelectorAll('tbody tr.cal-group-first'));
                if (!groupFirstRows.length) return;

                for (let gi = 0; gi < groupFirstRows.length; gi++) {
                    const startRow = groupFirstRows[gi];
                    const startIdx = rows.indexOf(startRow);
                    if (startIdx === -1) continue;
                    const nextStartRow = (gi + 1 < groupFirstRows.length) ? groupFirstRows[gi + 1] : null;
                    const endIdx = nextStartRow ? rows.indexOf(nextStartRow) : rows.length;
                    if (endIdx === -1) continue;

                    const groupRows = rows.slice(startIdx, endIdx);
                    if (!groupRows.length) continue;

                    const catCell = groupRows.map(r => r.querySelector('.cal-cat-label')).find(Boolean);
                    if (!catCell) continue;

                    if (catCell.parentElement) catCell.parentElement.removeChild(catCell);
                    groupRows.forEach(r => {
                        r.querySelectorAll('.cal-cat-label').forEach(c => c.remove());
                    });

                    const visibleRows = groupRows.filter(r => r.style.display !== 'none');
                    const targetRow = visibleRows.length ? visibleRows[0] : groupRows[0];
                    targetRow.insertBefore(catCell, targetRow.firstChild);
                    catCell.rowSpan = visibleRows.length ? visibleRows.length : groupRows.length;
                }
            }
            function applyCalendarFilters() {
                const levelToggles = document.querySelectorAll('[data-cal-filter-toggle]');
                const continentToggles = document.querySelectorAll('[data-cal-continent-toggle]');
                const surfaceToggles = document.querySelectorAll('[data-cal-surface-toggle]');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (!levelToggles.length && !continentToggles.length && !surfaceToggles.length && !gmToggle) return;

                const activeLevels = new Set();
                levelToggles.forEach(cb => { if (cb.checked) activeLevels.add(cb.dataset.calFilterToggle); });

                const activeContinents = new Set();
                continentToggles.forEach(cb => { if (cb.checked) activeContinents.add(cb.dataset.calContinentToggle); });

                const activeSurfaces = new Set();
                surfaceToggles.forEach(cb => { if (cb.checked) activeSurfaces.add(cb.dataset.calSurfaceToggle); });

                document.querySelectorAll('#view-calendar tr[data-cal-row-continent]').forEach(row => {
                    const rowCont = row.dataset.calRowContinent || '';
                    let show = true;
                    if (continentToggles.length && rowCont && !activeContinents.has(rowCont)) show = false;
                    row.style.display = show ? '' : 'none';
                });
                syncCalendarRowspans();

                document.querySelectorAll('#view-calendar [data-cal-filter]').forEach(el => {
                    const levelKey = el.dataset.calFilter || '';
                    const contKey = el.dataset.calContinent || '';
                    const surfKey = el.dataset.calSurface || '';

                    let visible = true;
                    if (levelToggles.length && levelKey && !activeLevels.has(levelKey)) visible = false;
                    if (continentToggles.length && contKey && !activeContinents.has(contKey)) visible = false;
                    if (surfaceToggles.length && surfKey && !activeSurfaces.has(surfKey)) visible = false;

                    el.style.display = visible ? '' : 'none';
                });

                const showGm = !gmToggle || gmToggle.getAttribute('aria-pressed') !== 'false';
                if (gmToggle) {
                    const gmAction = showGm ? 'Hide Quality' : 'Show Quality';
                    gmToggle.classList.toggle('active', showGm);
                    gmToggle.textContent = gmAction;
                    gmToggle.setAttribute('aria-label', gmAction + ' values');
                    gmToggle.title = gmAction + ' values';
                }
                document.querySelectorAll('#view-calendar .cal-gm-badge').forEach(badge => {
                    badge.style.display = showGm ? '' : 'none';
                });
                const gmLegend = document.querySelector('#view-calendar .cal-gm-legend');
                if (gmLegend) gmLegend.style.display = showGm ? '' : 'none';
                syncUrlStateForTab('calendar');
            }
            function initCalendarFilters() {
                if (calendarFiltersInitialized) {
                    applyCalendarFilters();
                    return;
                }
                initCalendarDropdowns();
                initCalendarHorizontalScroll();
                const toggles = document.querySelectorAll('[data-cal-filter-toggle], [data-cal-continent-toggle], [data-cal-surface-toggle]');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (!toggles.length && !gmToggle) return;
                toggles.forEach(cb => cb.addEventListener('change', applyCalendarFilters));
                if (gmToggle) {
                    gmToggle.addEventListener('click', function() {
                        const showGm = gmToggle.getAttribute('aria-pressed') !== 'true';
                        gmToggle.setAttribute('aria-pressed', showGm ? 'true' : 'false');
                        applyCalendarFilters();
                    });
                }
                calendarFiltersInitialized = true;
                applyCalendarFilters();
            }
            function _milestonesCell(items) {
                if (!Array.isArray(items) || !items.length) return '<span class="text-muted">—</span>';
                return items.map(item => `<div class="milestones-achiever"><span class="milestones-position">${item.position}-</span> ${escapeHtml(item.name)} <span class="milestones-date">(${escapeHtml(item.date)})</span></div>`).join('');
            }

            function _milestonesRenderHistorical() {
                const tbody = document.getElementById('milestones-historical-body');
                if (!tbody) return;
                const metricSelect = document.getElementById('milestones-historical-metric');
                const metric = metricSelect ? metricSelect.value : 'ranked';
                const rows = (window.WTARG_DATA.milestones || {}).historical || [];
                tbody.innerHTML = rows.map(row => `<tr><th scope="row">${row.year}</th><td>${_milestonesCell(row[metric])}</td></tr>`).join('');
            }

            function _milestonesRenderPointRows(tbody, rows, emptyMessage, includeDropDate = true) {
                if (!tbody) return;
                const columnCount = includeDropDate ? 5 : 4;
                if (!Array.isArray(rows) || !rows.length) {
                    tbody.innerHTML = `<tr><td colspan="${columnCount}" class="cell-state-info">${escapeHtml(emptyMessage)}</td></tr>`;
                    return;
                }
                tbody.innerHTML = rows.map(row => {
                    const dropDateCell = includeDropDate ? `<td>${escapeHtml(row.dropDate)}</td>` : '';
                    return `<tr><td>${escapeHtml(row.date)}</td><td>${escapeHtml(row.tournament)}</td><td>${escapeHtml(row.round)}</td><td>${row.points}</td>${dropDateCell}</tr>`;
                }).join('');
            }

            function renderMilestonesPlayer() {
                const select = document.getElementById('milestones-player-select');
                const selected = select ? select.value : '';
                const players = (window.WTARG_DATA.milestones || {}).active || [];
                const player = players.find(item => item.name === selected);
                const career = document.getElementById('milestones-career');
                const expiredSection = document.getElementById('milestones-expired-section');
                const liveTotal = document.getElementById('milestones-live-total');
                if (!player) {
                    if (career) career.hidden = true;
                    if (expiredSection) expiredSection.hidden = true;
                    if (liveTotal) liveTotal.textContent = 'Points: 0';
                    _milestonesRenderPointRows(document.getElementById('milestones-live-body'), [], 'Select a player to view their results');
                    return;
                }
                if (career) career.hidden = false;
                if (liveTotal) liveTotal.textContent = `Points: ${player.livePoints}`;
                _milestonesRenderPointRows(document.getElementById('milestones-live-body'), player.liveRows, 'No tournaments found in the current ranking window.');
                const lastRanked = document.getElementById('milestones-last-ranked');
                if (lastRanked) {
                    lastRanked.hidden = !player.lastRankedWeek;
                    lastRanked.textContent = player.lastRankedWeek ? `Last week with WTA ranking: ${player.lastRankedWeek}` : '';
                }
                const everTotal = document.getElementById('milestones-ever-total');
                if (everTotal) everTotal.textContent = `Total ever WTA points earned: ${player.totalEverPoints}`;
                const expiredRows = Array.isArray(player.expiredRows) ? player.expiredRows : [];
                if (expiredSection) expiredSection.hidden = !expiredRows.length;
                if (expiredRows.length) {
                    _milestonesRenderPointRows(document.getElementById('milestones-expired-body'), expiredRows, '', false);
                }
            }

            function switchMilestonesTab(tabName) {
                const historical = tabName !== 'active';
                const historicalPanel = document.getElementById('milestones-historical');
                const activePanel = document.getElementById('milestones-active');
                const historicalButton = document.getElementById('milestones-btn-historical');
                const activeButton = document.getElementById('milestones-btn-active');
                if (historicalPanel) historicalPanel.hidden = !historical;
                if (activePanel) activePanel.hidden = historical;
                if (historicalButton) {
                    historicalButton.classList.toggle('active', historical);
                    historicalButton.setAttribute('aria-selected', String(historical));
                }
                if (activeButton) {
                    activeButton.classList.toggle('active', !historical);
                    activeButton.setAttribute('aria-selected', String(!historical));
                }
                if (historical) _milestonesRenderHistorical();
            }

            function initInformationPage() {
                _milestonesRenderHistorical();
                const select = document.getElementById('milestones-player-select');
                if (select && select.dataset.initialized !== '1') {
                    select.dataset.initialized = '1';
                    $(select).select2({ placeholder: 'Select Player...', allowClear: true, width: '100%' });
                    $(select).on('change', renderMilestonesPlayer);
                }
            }
            function switchTab(tabName) {
                if (tabName === 'home' && homeLocked) return;
                if (tabName !== 'history') closeHistoryFilters(false);
                _currentTabName = (tabName || 'home').toString().trim().toLowerCase() || 'home';
                _urlStateSwitching = true;
                document.querySelectorAll('.menu-item').forEach(el => {
                    el.classList.remove('active');
                    el.removeAttribute('aria-current');
                });
                const btn = document.getElementById('btn-' + tabName);
                if (btn) {
                    btn.classList.add('active');
                    btn.setAttribute('aria-current', 'page');
                }

                if (tabName !== 'home') {
                    homeLocked = true;
                    const homeView = document.getElementById('view-home');
                    if (homeView) homeView.style.display = 'none';
                    document.body.classList.remove('home-mode');
                } else {
                    document.body.classList.add('home-mode');
                }

                if (tabName === 'calendar') {
                    document.body.classList.add('calendar-mode');
                } else {
                    document.body.classList.remove('calendar-mode');
                }

                document.getElementById('view-upcoming').style.display = (tabName === 'upcoming') ? 'flex' : 'none';
                document.getElementById('view-entrylists').style.display = (tabName === 'entrylists') ? 'flex' : 'none';

                document.getElementById('view-rankings').style.display = (tabName === 'rankings') ? 'flex' : 'none';
                document.getElementById('view-history').style.display = (tabName === 'history') ? 'flex' : 'none';
                document.getElementById('view-fedbcup').style.display = (tabName === 'fedbcup') ? 'flex' : 'none';
                document.getElementById('view-calendar').style.display = (tabName === 'calendar') ? 'flex' : 'none';
                document.getElementById('view-roadtogs').style.display = (tabName === 'roadtogs') ? 'flex' : 'none';
                document.getElementById('view-draws').style.display = (tabName === 'draws') ? 'block' : 'none';
                document.getElementById('view-tstrength').style.display = (tabName === 'tstrength') ? 'flex' : 'none';
                document.getElementById('view-information').style.display = (tabName === 'information') ? 'flex' : 'none';
                if (tabName === 'history') setHistorySubpage(HISTORY_SUBPAGE_MATCH);

                if (tabName === 'entrylists') {
                    setEntryMenuCollapsed(false);
                    updateEntryMenuLabels();
                    updateEntryList();
                }
                if (tabName === 'draws') updateDraw();
                if (tabName === 'calendar') initCalendarFilters();
                if (tabName === 'rankings') initRankingsIfEmpty();
                if (tabName === 'roadtogs') initRoadToGS();
                if (tabName === 'information') initInformationPage();

                applyMobileHistoryLayout();
                syncEntryMenuToggle();

                if (tabName !== 'home') {
                    try { localStorage.setItem('lastTab', tabName); } catch(e) {}
                }

                _urlStateSwitching = false;
                restoreAndSyncUrlStateForTab(tabName);

                // Close mobile menu after selecting
                if (window.innerWidth <= 768) {
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }
            }

            document.body.classList.add('home-mode');
            document.addEventListener('DOMContentLoaded', initCalendarFilters);

            const BJKC_PLAYERS = window.WTARG_DATA.bjkcPlayers;

            (function() {
                const sel = document.getElementById('fedbcup-player-filter');
                if (sel) {
                    BJKC_PLAYERS.forEach(function(p) {
                        const o = document.createElement('option');
                        o.value = p;
                        o.textContent = p;
                        sel.appendChild(o);
                    });
                }
                updateFedBjkRecord('');
            })();

            function switchFedBjkTab(subTab) {
                document.getElementById('view-fedbcup').classList.toggle('fedbcup-series-active', subTab === 'series');
                document.getElementById('fedbcup-view-players').style.display = (subTab === 'players') ? '' : 'none';
                document.getElementById('fedbcup-view-captains').style.display = (subTab === 'captains') ? '' : 'none';
                document.getElementById('fedbcup-view-series').style.display = (subTab === 'series') ? '' : 'none';
                document.getElementById('fedbcup-btn-players').classList.toggle('active', subTab === 'players');
                document.getElementById('fedbcup-btn-captains').classList.toggle('active', subTab === 'captains');
                document.getElementById('fedbcup-btn-series').classList.toggle('active', subTab === 'series');
                const filterLeft = document.getElementById('fedbcup-filter-left');
                const recordRight = document.getElementById('fedbcup-record-right');
                const vis = (subTab === 'series') ? 'visible' : 'hidden';
                if (filterLeft) filterLeft.style.visibility = vis;
                if (recordRight) recordRight.style.visibility = vis;
                syncUrlStateForTab('fedbcup');
            }

            function filterFedBjkPlayer() {
                const sel = document.getElementById('fedbcup-player-filter');
                const player = sel ? sel.value : '';
                const visibleBlocks = [];
                document.querySelectorAll('.bjkc-series-table tbody tr').forEach(function(tr) {
                    if (!player) { tr.style.display = ''; return; }
                    const players = (tr.getAttribute('data-player') || '').split('|');
                    tr.style.display = players.includes(player) ? '' : 'none';
                });
                document.querySelectorAll('.bjkc-series-block').forEach(function(block) {
                    if (!player) {
                        block.style.display = '';
                        visibleBlocks.push(block);
                        return;
                    }
                    const rows = block.querySelectorAll('.bjkc-series-table tbody tr');
                    const visible = Array.from(rows).some(function(r) { return r.style.display !== 'none'; });
                    block.style.display = visible ? '' : 'none';
                    if (visible) visibleBlocks.push(block);
                });
                visibleBlocks.forEach(function(block, index) { block.open = index === 0; });
                updateFedBjkRecord(player);
                syncUrlStateForTab('fedbcup');
            }

            function updateFedBjkRecord(player) {
                let sw = 0, sl = 0, dw = 0, dl = 0;
                document.querySelectorAll('.bjkc-series-table tbody tr').forEach(function(tr) {
                    const result = tr.getAttribute('data-result');
                    if (!result) return;
                    const type = tr.getAttribute('data-type');
                    const players = (tr.getAttribute('data-player') || '').split('|');
                    if (player && !players.includes(player)) return;
                    if (type === 'S') { if (result === 'W') sw++; else sl++; }
                    else { if (result === 'W') dw++; else dl++; }
                });
                const rec = document.getElementById('fedbcup-record');
                if (rec) {
                    let text = 'S: ' + sw + '-' + sl;
                    if (dw + dl > 0) text += ' | D: ' + dw + '-' + dl;
                    rec.textContent = text;
                }
            }

            function applyMobileHistoryLayout() {
                const historyLayout = document.querySelector('#view-history .history-layout');
                if (!historyLayout) return;

                const filterPanel = historyLayout.querySelector('.filter-panel');
                const historyContent = historyLayout.querySelector('.history-content');
                if (!filterPanel || !historyContent) return;

                // The panel stays in the desktop rail in the DOM; mobile CSS presents it as a sheet.
                if (historyContent.contains(filterPanel)) {
                    historyLayout.insertBefore(filterPanel, historyContent);
                }
                syncHistoryFilterSheetMode();
            }

            let _historyFilterReturnFocus = null;

            function getHistoryActiveFilterCount(filterState) {
                const state = filterState || getHistoryFilterSelectionState();
                const multiValueKeys = [
                    'surfaces', 'rounds', 'results', 'years', 'categories',
                    'opponentCountries', 'playerEntries', 'seeds', 'matchTypes'
                ];
                let count = multiValueKeys.reduce((total, key) => total + state[key].length, 0);
                if (state.tournament) count += 1;
                if (state.opponent) count += 1;
                if (state.asRankVal !== null) count += 1;
                if (state.vsRankVal !== null) count += 1;
                return count;
            }

            function updateHistoryMobileFilterButton(filterState) {
                const button = document.getElementById('history-mobile-filter-btn');
                const label = document.getElementById('history-mobile-filter-label');
                if (!button || !label) return;
                const count = getHistoryActiveFilterCount(filterState);
                label.textContent = count ? `Filters · ${count}` : 'Filters';
                button.classList.toggle('has-active-filters', count > 0);
                button.setAttribute('aria-label', count ? `Filters, ${count} active` : 'Filters, no active filters');
            }

            function syncHistoryFilterSheetMode() {
                const panel = document.getElementById('history-filter-panel');
                const button = document.getElementById('history-mobile-filter-btn');
                if (!panel || !button) return;
                const mobile = window.innerWidth <= 768;
                const available = mobile && historySubpage === HISTORY_SUBPAGE_MATCH;
                if (!available) document.body.classList.remove('history-filters-open');
                const isOpen = available && document.body.classList.contains('history-filters-open');
                button.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
                if (mobile) {
                    panel.setAttribute('role', 'dialog');
                    panel.setAttribute('aria-modal', 'true');
                    panel.setAttribute('aria-labelledby', 'history-filter-sheet-title');
                    panel.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
                } else {
                    panel.removeAttribute('role');
                    panel.removeAttribute('aria-modal');
                    panel.removeAttribute('aria-labelledby');
                    panel.removeAttribute('aria-hidden');
                }
            }

            function openHistoryFilters() {
                if (window.innerWidth > 768 || historySubpage !== HISTORY_SUBPAGE_MATCH) return;
                _historyFilterReturnFocus = document.activeElement;
                document.body.classList.add('history-filters-open');
                syncHistoryFilterSheetMode();
                const closeButton = document.querySelector('.history-filter-sheet-close');
                if (closeButton) requestAnimationFrame(() => closeButton.focus({ preventScroll: true }));
            }

            function closeHistoryFilters(restoreFocus = true) {
                const wasOpen = document.body.classList.contains('history-filters-open');
                document.body.classList.remove('history-filters-open');
                syncHistoryFilterSheetMode();
                if (restoreFocus && wasOpen && _historyFilterReturnFocus && document.contains(_historyFilterReturnFocus)) {
                    _historyFilterReturnFocus.focus({ preventScroll: true });
                }
                _historyFilterReturnFocus = null;
            }

            document.addEventListener('keydown', function(event) {
                if (event.key === 'Escape' && document.body.classList.contains('history-filters-open')) {
                    closeHistoryFilters();
                }
            });

            function reverseScore(score) {
                if (!score) return '';
                return score.split(' ').map(set => {
                    const m = set.match(/^(\d+)-(\d+)(.*)$/);
                    if (!m) return set;
                    return m[2] + '-' + m[1] + m[3];
                }).join(' ');
            }

            function formatSeed(seed) {
                if (seed === null || seed === undefined) return '';
                const text = String(seed).trim();
                if (!text) return '';
                const num = Number(text);
                if (!Number.isNaN(num) && Number.isInteger(num)) {
                    return String(num);
                }
                return text;
            }

            function buildPrefix(seed, entry) {
                const parts = [];
                const formattedSeed = formatSeed(seed);
                if (formattedSeed) parts.push(formattedSeed);
                if (entry) parts.push(entry);
                if (parts.length === 0) return '';
                return '(' + parts.join('/') + ') ';
            }

            function buildHistoryPlayerCell(rank, country, seed, entry, name) {
                const rankText = String(rank || '').trim();
                const rankHtml = rankText && rankText !== '-' ? `#${rankText}` : '';
                const countryText = String(country || '').trim();
                const flagHtml = countryText && countryText !== '-' ? countryFlagHistory(countryText, false) : '';
                return `<span class="history-player-cell"><span class="history-player-rank">${rankHtml}</span>${
                    flagHtml ? `<span class="history-player-flag">${flagHtml}</span>` : ''
                }<span class="history-player-name">${buildPrefix(seed, entry) + name}</span></span>`;
            }

            const _drItfDrawLookup = {};
            itfDrawSizes.forEach(t => {
                const key = (t.tournamentName || '') + '|' + (t.date || '');
                _drItfDrawLookup[key] = t.mainDrawSize;
                const weekMatch = (t.tournamentName || '').match(/^(.+?)\s*\(Week \d+\)$/);
                if (weekMatch) {
                    _drItfDrawLookup[weekMatch[1].trim() + '|' + (t.date || '')] = t.mainDrawSize;
                }
            });
            const _drWtaDrawLookup = {};
            wtaDrawSizes.forEach(t => {
                if (!t.tournamentId) return;
                const normId = String(parseInt(t.tournamentId) || t.tournamentId);
                _drWtaDrawLookup[normId] = t.mainDrawSize;
            });
            const _drCategoryDrawSize = {
                'GS': 128, 'WTA 1000': 96,
                'WTA 500': 32, 'WTA 250': 32, 'WTA 125': 32,
                '125K': 32, '125K Series': 32,
                'W100': 32, 'W75': 32, 'W50': 32, 'W35': 32, 'W15': 32
            };
            function _drHistoricalKeys(tournamentId, date, tournamentName) {
                const year = String(date || '').slice(0, 4);
                if (!year) return [];
                const keys = [];
                if (tournamentId) {
                    const normId = String(parseInt(tournamentId) || tournamentId).trim();
                    if (normId) keys.push('id|' + normId + '|' + year);
                }
                const normName = String(tournamentName || '').trim().toUpperCase();
                if (normName) keys.push('name|' + normName + '|' + year);
                return keys;
            }
            function _drHistoricalDrawSize(tournamentId, date, tournamentName) {
                const keys = _drHistoricalKeys(tournamentId, date, tournamentName);
                for (const key of keys) {
                    const slotSize = historicalDrawSlots[key];
                    if (slotSize) return slotSize;
                }
                return null;
            }
            function _drResolveDrawSize(tournamentId, date, tournamentName, category, matchType) {
                if ((matchType === 'WTA' || matchType === 'GS') && tournamentId) {
                    const normId = String(parseInt(tournamentId) || tournamentId);
                    const sz = _drWtaDrawLookup[normId];
                    if (sz) return sz;
                }
                if (matchType === 'WTA' || matchType === 'GS') {
                    const sz = _drHistoricalDrawSize(tournamentId, date, tournamentName);
                    if (sz) return sz;
                }
                if (matchType === 'ITF') {
                    const sz = _drItfDrawLookup[(tournamentName || '') + '|' + (date || '')];
                    if (sz) return sz;
                    const historicalSz = _drHistoricalDrawSize(tournamentId, date, tournamentName);
                    if (historicalSz) return historicalSz;
                }
                return _drCategoryDrawSize[category] || 32;
            }

            function displayRound(round, tournamentId, date, tournamentName, category, matchType, draw) {
                if (!round) return '';
                const roundText = (round || '').toString().trim();
                const roundUpper = roundText.toUpperCase();
                if (roundUpper.startsWith('ROUND ROBIN')) return 'RR';
                const qualifyingRound = roundUpper.match(/^Q(?:R)?(\d+)$/);
                if (qualifyingRound) return 'QR' + qualifyingRound[1];
                // Qualifying draw: keep one canonical QR1/QR2/QR3 notation.
                if (draw === 'Q') {
                    const _qMap = {'1st Round':'QR1','2nd Round':'QR2','3rd Round':'QR3','4th Round':'QR4'};
                    return _qMap[roundText] || roundText;
                }
                // Team/non-individual draws: normalize round names
                if (draw !== 'M') {
                    const _tMap = {'Round Robin':'RR','Last 32':'R32','Last 16':'R16','Last 8':'QF','Quarter Finals':'QF','Semi Finals':'SF','Final':'F'};
                    return _tMap[roundText] || roundText;
                }
                if (roundText === 'Final') return 'F';
                if (roundText === 'Semi-finals' || roundText === 'Semi Finals') return 'SF';
                if (roundText === 'Quarter-finals' || roundText === 'Quarter Finals') return 'QF';
                const drawSize = _drResolveDrawSize(tournamentId, date, tournamentName, category, matchType);
                const _ordinalNum = {'1st Round':1,'2nd Round':2,'3rd Round':3,'4th Round':4,'5th Round':5}[roundText];
                if (_ordinalNum !== undefined) {
                    const nextPow2 = Math.pow(2, Math.ceil(Math.log2(drawSize)));
                    const n = nextPow2 / Math.pow(2, _ordinalNum - 1);
                    if (n <= 2) return 'F';
                    if (n <= 4) return 'SF';
                    if (n <= 8) return 'QF';
                    return 'R' + n;
                }
                return roundText;
            }

            // Format date string to yyyy-MM-dd
            function formatDate(dateStr) {
                if (!dateStr) return '';
                const parts = dateStr.split('/');
                if (parts.length === 3) {
                    return parts[2] + '-' + parts[1].padStart(2, '0') + '-' + parts[0].padStart(2, '0');
                }
                const d = new Date(dateStr);
                if (isNaN(d)) return dateStr;
                return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
            }

            // Helper function to get display name from player mapping
            // Build reverse lookup cache for O(1) name resolution
            const _displayNameCache = {};
            const _displayNameBySourceId = {wta: {}, itf: {}, bjkc: {}};
            const _ambiguousNameKeys = new Set();
            (function() {
                function registerSourceId(source, playerId, canonicalName) {
                    const sourceKey = (source || '').toString().trim().toLowerCase();
                    const id = (playerId || '').toString().trim();
                    const display = (canonicalName || '').toString().trim();
                    if (!sourceKey || !id || !display || !_displayNameBySourceId[sourceKey]) return;
                    _displayNameBySourceId[sourceKey][id] = display;
                }

                function registerName(canonicalName, rawName) {
                    const canonical = (canonicalName || '').toString().trim();
                    const raw = (rawName || '').toString().trim();
                    if (!canonical && !raw) return;
                    const display = canonical || raw;
                    _displayNameCache[display.toUpperCase()] = display;
                    if (raw) {
                        const rawKey = raw.toUpperCase();
                        if (_ambiguousNameKeys.has(rawKey)) return;
                        const previous = _displayNameCache[rawKey];
                        if (previous && previous !== display) {
                            delete _displayNameCache[rawKey];
                            _ambiguousNameKeys.add(rawKey);
                        } else {
                            _displayNameCache[rawKey] = display;
                        }
                    }
                }

                if (Array.isArray(playerMapping)) {
                    for (const item of playerMapping) {
                        if (!item || typeof item !== 'object') continue;
                        const canonical = item.presentation_name || item.display_name || item.wta_name || item.itf_name || item.bjkc_name || '';
                        if (!canonical) continue;
                        registerSourceId('wta', item.wta_id, canonical);
                        registerSourceId('itf', item.itf_id, canonical);
                        registerSourceId('bjkc', item.bjkc_id, canonical);
                        (item.additional_wta_ids || []).forEach(id => registerSourceId('wta', id, canonical));
                        (item.additional_itf_ids || []).forEach(id => registerSourceId('itf', id, canonical));
                        (item.additional_bjkc_ids || []).forEach(id => registerSourceId('bjkc', id, canonical));
                        registerName(canonical, canonical);
                        registerName(canonical, item.display_name);
                        registerName(canonical, item.wta_name);
                        registerName(canonical, item.itf_name);
                        registerName(canonical, item.bjkc_name);
                        if (Array.isArray(item.aliases)) {
                            for (const alias of item.aliases) {
                                registerName(canonical, alias);
                            }
                        }
                    }
                } else {
                    for (const [displayName, aliases] of Object.entries(playerMapping)) {
                        if (!displayName) continue;
                        registerName(displayName, displayName);
                        if (!Array.isArray(aliases)) continue;
                        for (const alias of aliases) {
                            registerName(displayName, alias);
                        }
                    }
                }
            })();

            function getDisplayNameForIdentity(source, playerId, rawName) {
                const sourceKey = (source || '').toString().trim().toLowerCase();
                const id = (playerId || '').toString().trim();
                const byId = _displayNameBySourceId[sourceKey];
                if (byId && id && byId[id]) return byId[id];
                return getDisplayName((rawName || '').toString().toUpperCase());
            }

            function historyIdentitySource(row) {
                const value = ((row && row.MATCH_TYPE) || '').toString().trim().toUpperCase();
                if (value === 'ITF') return 'itf';
                if (['WTA', 'GS', 'OG', 'UNITED CUP'].includes(value)) return 'wta';
                if (value.includes('BJK') || value.includes('FED CUP')) return 'bjkc';
                return value.toLowerCase();
            }

            function getDisplayName(upperCaseName) {
                const normalizedKey = (upperCaseName || '').toString().trim().toUpperCase();
                const cached = _displayNameCache[normalizedKey];
                if (cached) return cached;
                // If not found, convert to title case (handling hyphens)
                const result = normalizedKey.split(' ').map(word => {
                    if (word.includes('-')) {
                        return word.split('-').map(part =>
                            part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()
                        ).join('-');
                    }
                    return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
                }).join(' ');
                _displayNameCache[normalizedKey] = result;
                return result;
            }

            function normalizeHistoryPlayerSelect() {
                const select = document.getElementById('playerHistorySelect');
                if (!select) return;

                const currentValue = (select.value || '').toString();
                const currentUpper = currentValue.toUpperCase();
                const seen = new Set();
                const fragment = document.createDocumentFragment();

                function appendOption(value, text) {
                    const option = document.createElement('option');
                    option.value = value;
                    option.textContent = text;
                    fragment.appendChild(option);
                }

                appendOption('', 'Select Player...');
                appendOption('__ALL__', 'ALL PLAYERS');

                Array.from(select.options).forEach(option => {
                    const value = (option.value || '').toString().trim();
                    if (!value || value === '__ALL__' || value === 'Select Player...') return;
                    const canonical = getDisplayName(value.toUpperCase());
                    const canonicalUpper = canonical.toUpperCase();
                    if (seen.has(canonicalUpper)) return;
                    seen.add(canonicalUpper);
                    appendOption(canonical, canonical);
                });

                select.innerHTML = '';
                select.appendChild(fragment);

                if (currentUpper === '__ALL__') {
                    select.value = '__ALL__';
                } else if (currentUpper) {
                    select.value = getDisplayName(currentUpper);
                }

                _historyPlayerUniverse = null;
                _historyPlayerUniverseUpper = null;
            }

            function getNormalizedPlayerSelection(selectId) {
                const select = document.getElementById(selectId);
                const value = select ? (select.value || '').toString().trim() : '';
                if (!value || value === '__ALL__') return value;
                return getDisplayName(value.toUpperCase()).toUpperCase();
            }

            $(document).ready(function() {
                // Initialize sidebar state for mobile
                if (window.innerWidth <= 768) {
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }

                normalizeHistoryPlayerSelect();

                $('#playerHistorySelect').select2({
                    placeholder: 'Select a player...',
                    allowClear: true,
                    width: '100%'
                });

                $('#playerHistorySelect').on('change', function() {
                    filterHistoryByPlayer();
                });

                renderHistoryTable();
                renderMilestonesTable();
                setHistorySubpage(historySubpage);
                applyMobileHistoryLayout();
                updateHistoryMobileFilterButton();

                // Handle window resize
                window.addEventListener('resize', function() {
                    if (window.innerWidth > 768) {
                        document.getElementById('sidebar').classList.remove('mobile-hidden');
                    } else {
                        document.getElementById('sidebar').classList.add('mobile-hidden');
                    }
                    applyMobileHistoryLayout();
                });
            });

            function filter() {
                const q = document.getElementById('s').value.toLowerCase();
                document.querySelectorAll('#tb tr').forEach(row => {
                    const matches = row.getAttribute('data-name').includes(q);
                    row.classList.toggle('hidden', !matches);
                });
                syncUrlStateForTab('upcoming');
            }
            let showArgOnly = false;
            function toggleRankingsScope() {
                showArgOnly = !showArgOnly;
                const btn = document.getElementById('rankings-toggle-btn');
                const view = document.getElementById('view-rankings');
                if (btn) btn.innerHTML = showArgOnly ? 'Show ALL' : 'Show <img class="btn-flag-icon" src="assets/argentina.png" alt="ARG">';
                if (view) view.classList.toggle('rankings-show-all', !showArgOnly);
                filterRankings();
            }
            function updateRankingsRowParity() {
                let visibleIndex = 0;
                document.querySelectorAll('#rankings-body tr').forEach(row => {
                    row.classList.remove('rankings-visible-odd', 'rankings-visible-even');
                    if (row.classList.contains('hidden') || row.classList.contains('rankings-system-row')) return;
                    row.classList.add(visibleIndex % 2 === 0 ? 'rankings-visible-odd' : 'rankings-visible-even');
                    visibleIndex += 1;
                });
            }
            function filterRankings() {
                const q = document.getElementById('rankings-search').value.toLowerCase();
                document.querySelectorAll('#rankings-body tr').forEach(row => {
                    if (row.classList.contains('rankings-system-row')) {
                        row.classList.remove('hidden');
                        return;
                    }
                    const text = row.textContent.toLowerCase();
                    const nat = row.getAttribute('data-country') || (row.children[2] ? row.children[2].textContent.trim().toUpperCase() : '');
                    const matchesSearch = text.includes(q);
                    const matchesCountry = !showArgOnly || nat === 'ARG';
                    row.classList.toggle('hidden', !(matchesSearch && matchesCountry));
                });
                updateRankingsRowParity();
                syncUrlStateForTab('rankings');
            }
            const _rankingBundleCaches = {};
            const _rankingBundlePromises = {};
            const _rankingsLatestDate = window.WTARG_DATA.rankingsLatestDate;
            function _rankingBundleForDate(dateStr) {
                if (dateStr === _rankingsLatestDate) {
                    return { file: 'data/wta_rankings_latest_bundle.js', globalName: '__WTA_RANKINGS_LATEST__' };
                }
                const year = parseInt(String(dateStr || '').split('-')[0]);
                return {
                    file: `data/wta_rankings_${year}_bundle.js`,
                    globalName: `__WTA_RANKINGS_${year}__`
                };
            }
            function _loadRankingDataForDate(dateStr) {
                const info = _rankingBundleForDate(dateStr);
                const file = info.file;
                if (_rankingBundleCaches[file]) return Promise.resolve(_rankingBundleCaches[file]);
                if (_rankingBundlePromises[file]) return _rankingBundlePromises[file];
                const existing = window[info.globalName];
                if (existing && typeof existing === 'object') {
                    _rankingBundleCaches[file] = existing;
                    return Promise.resolve(existing);
                }
                _rankingBundlePromises[file] = _loadLocalScriptOnce(file)
                    .then(() => {
                        const data = window[info.globalName];
                        if (!data || typeof data !== 'object') {
                            throw new Error('Ranking bundle did not initialize: ' + file);
                        }
                        _rankingBundleCaches[file] = data;
                        _rankingBundlePromises[file] = null;
                        return data;
                    })
                    .catch(err => {
                        console.error('Failed to load ' + file + ':', err);
                        _rankingBundlePromises[file] = null;
                        throw err;
                    });
                return _rankingBundlePromises[file];
            }
            function _renderRankingRows(players) {
                const tbody = document.getElementById('rankings-body');
                let html = '';
                players.forEach(p => {
                    const dob = (p.d || '').split('T')[0];
                    const name = (p.n || '').toLowerCase().replace(/(^|\s)(\S)/g, (_, b, c) => b + c.toUpperCase());
                    html += `<tr data-country="${(p.c||'').toUpperCase()}"><td>${p.r || ''}</td><td style="text-align:left;font-weight:bold;">${countryFlag(p.c || '', false)} ${name}</td><td>${p.pts || ''}</td><td>${dob}</td></tr>`;
                });
                tbody.innerHTML = html;
                filterRankings();
            }
            const _rankingsDatesIndex = window.WTARG_DATA.rankingsDatesIndex;
            const _rankingMonthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            function _populateRankingMonths(year, selectMonth, selectDay) {
                const sel = document.getElementById('rankings-month-select');
                const months = Object.keys(_rankingsDatesIndex[year] || {}).map(Number).sort((a,b)=>a-b);
                const chosenM = (selectMonth != null && months.includes(+selectMonth)) ? +selectMonth : months[months.length-1];
                sel.innerHTML = months.map(m => {
                    const isSel = (m === chosenM) ? ' selected' : '';
                    return `<option value="${m}"${isSel}>${_rankingMonthNames[m-1]}</option>`;
                }).join('');
                _populateRankingDays(year, chosenM, selectDay);
            }
            function _populateRankingDays(year, monthNum, selectDay) {
                const sel = document.getElementById('rankings-day-select');
                const days = ((_rankingsDatesIndex[year] || {})[String(monthNum)] || []).slice().sort((a,b)=>a-b);
                const chosenD = (selectDay != null && days.includes(+selectDay)) ? +selectDay : days[days.length-1];
                sel.innerHTML = days.map(d => {
                    const isSel = (d === chosenD) ? ' selected' : '';
                    return `<option value="${d}"${isSel}>${d}</option>`;
                }).join('');
            }
            function onRankingYearChange(year) {
                _populateRankingMonths(year, null, null);
            }
            function onRankingMonthChange() {
                const year = document.getElementById('rankings-year-select').value;
                const month = +document.getElementById('rankings-month-select').value;
                _populateRankingDays(year, month, null);
            }
            function applyRankingSelection() {
                const year = document.getElementById('rankings-year-select').value;
                const month = document.getElementById('rankings-month-select').value;
                const day = document.getElementById('rankings-day-select').value;
                if (!year || !month || !day) return;
                const mm = month.toString().padStart(2,'0');
                const dd = day.toString().padStart(2,'0');
                switchRankingWeek(`${year}-${mm}-${dd}`);
            }
            function _renderRankingSkeleton(rowCount) {
                const tbody = document.getElementById('rankings-body');
                if (!tbody) return;
                const widths = ['40%', '80%', '60%', '65%', '55%', '75%', '50%', '70%', '45%', '60%'];
                let html = '';
                for (let i = 0; i < rowCount; i++) {
                    const w = widths[i % widths.length];
                    html += '<tr class="skeleton-row">'
                          + '<td><span class="skeleton-bar" style="width:24px"></span></td>'
                          + '<td><span class="skeleton-bar" style="width:' + w + '"></span></td>'
                          + '<td><span class="skeleton-bar" style="width:40px"></span></td>'
                          + '<td><span class="skeleton-bar" style="width:60px"></span></td>'
                          + '</tr>';
                }
                tbody.innerHTML = html;
            }
            function switchRankingWeek(dateStr) {
                const controls = ['rankings-year-select','rankings-month-select','rankings-day-select','rankings-load-btn'].map(id => document.getElementById(id));
                controls.forEach(el => { if(el) { el.disabled = true; el.style.opacity = '0.5'; } });
                _renderRankingSkeleton(15);
                return _loadRankingDataForDate(dateStr)
                    .then(data => {
                        const players = data[dateStr];
                        if (players) _renderRankingRows(players);
                        else document.getElementById('rankings-body').innerHTML = '<tr class="rankings-system-row"><td colspan="4" class="cell-state-info rankings-system-row">No rankings found for the selected date.</td></tr>';
                    })
                    .catch(err => {
                        console.error('Failed to load rankings data:', err);
                        document.getElementById('rankings-body').innerHTML = '<tr class="rankings-system-row"><td colspan="4" class="cell-state-error rankings-system-row">Failed to load local rankings data. Please regenerate the site and reopen it.</td></tr>';
                    })
                    .finally(() => {
                        controls.forEach(el => { if(el) { el.disabled = false; el.style.opacity = '1'; } });
                        syncUrlStateForTab('rankings');
                    });
            }
            let _rankingsInitialized = false;
            function initRankingsIfEmpty() {
                if (_rankingsInitialized) return;
                _rankingsInitialized = true;
                applyRankingSelection();
            }
            _populateRankingMonths(window.WTARG_DATA.rankingsLatestYear, window.WTARG_DATA.rankingsLatestMonth, window.WTARG_DATA.rankingsLatestDay);
            function filterNational() {
                const q = document.getElementById('national-search').value.toLowerCase();
                document.querySelectorAll('#national-body tr').forEach(row => {
                    const text = row.textContent.toLowerCase();
                    row.classList.toggle('hidden', !text.includes(q));
                });
            }
            function entryMenuNameForItem(el) {
                if (!el) return '';
                const nameEl = el.querySelector('.entry-menu-name');
                return nameEl ? nameEl.textContent.trim() : el.textContent.trim();
            }

            function entryByPos(a, b) {
                return (Number(a.pos_num ?? 999) - Number(b.pos_num ?? 999))
                    || String(a.name || '').localeCompare(String(b.name || ''));
            }

            function entrySortEL(list) {
                const byRank = (a, b) => {
                    const rankScore = p => {
                        const r = String(p.rank || '');
                        const m = r.match(/\d+(\.\d+)?/);
                        const n = m ? parseFloat(m[0]) : 9999;
                        if (r.startsWith('WTN')) return [2, n];
                        if (r.startsWith('ITF')) return [1, n];
                        return [0, n];
                    };
                    const [ta, na] = rankScore(a);
                    const [tb, nb] = rankScore(b);
                    return (ta - tb) || (na - nb) || String(a.name || '').localeCompare(String(b.name || ''));
                };
                const mdo = list.filter(p => p.pos === 'MDO').sort(byRank);
                const numbered = list.filter(p => p.pos !== 'MDO').sort(entryByPos);
                if (mdo.length === 0) return numbered;
                const used = new Set(numbered.map(p => p.pos_num));
                const maxPos = numbered.length > 0 ? Math.max(...numbered.map(p => p.pos_num)) : 0;
                const gaps = [];
                for (let i = 1; i <= maxPos; i++) { if (!used.has(i)) gaps.push(i); }
                const result = [];
                let mi = 0, gi = 0, overflow = 1;
                for (const p of numbered) {
                    while (mi < mdo.length && byRank(mdo[mi], p) < 0) {
                        const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                        result.push({...mdo[mi++], pos_num});
                    }
                    result.push(p);
                }
                while (mi < mdo.length) {
                    const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                    result.push({...mdo[mi++], pos_num});
                }
                return result;
            }

            function entryGetDisplayMain(players, isITF) {
                const safe = (players || []).filter(Boolean);
                const main = safe.filter(p => p.type === 'MAIN').sort(entryByPos);
                if (!_prioFilterActive || !isITF) return main;
                const qual = entrySortEL(safe.filter(p => p.type === 'QUAL'));
                const alt = entrySortEL(safe.filter(p => p.type === 'ALT'));
                const mainJRPrio1 = main.filter(p => (p.entry === 'JR' || p.entry === 'JA') && p.priority === '1');
                const mainRegular = main.filter(p => p.entry !== 'JR' && p.entry !== 'JA');
                const regularSpots = main.length - mainJRPrio1.length;
                const pool = [
                    ...mainRegular.filter(p => p.priority === '1'),
                    ...qual.filter(p => p.priority === '1'),
                    ...alt.filter(p => p.priority === '1'),
                ];
                return [
                    ...pool.slice(0, regularSpots),
                    ...mainJRPrio1,
                ];
            }

            const _itfMainPlaceholderNames = new Set(['(Available Slot)', '(Special Exempt)']);

            function entryFillITFMainPlaceholders(mainPlayers, qualPlayers) {
                const main = (mainPlayers || []).filter(Boolean);
                const quals = (qualPlayers || []).filter(Boolean);
                if (!main.length || !quals.length) return main;

                let qualIndex = 0;
                let replaced = false;
                const filled = main.map(p => {
                    if (_itfMainPlaceholderNames.has(String(p.name || '')) && qualIndex < quals.length) {
                        replaced = true;
                        return quals[qualIndex++];
                    }
                    return p;
                });

                return replaced ? filled : main;
            }

            function entryRankToNumber(rank) {
                if (Number.isFinite(rank)) return rank;
                if (rank === null || rank === undefined) return 2000;
                const text = String(rank).trim();
                if (!text || text === '-') return 2000;
                // Special/non-DA entries display their WTA rank in parentheses,
                // for example "CA (431)" or "JA (365)". Use that WTA rank in GM.
                const specialRank = text.match(/^[A-Z][A-Z0-9-]*\s*\((\d+(?:\.\d+)?)\)$/i);
                if (specialRank) {
                    const value = parseFloat(specialRank[1]);
                    return Number.isFinite(value) && value > 0 ? value : 2000;
                }
                // Only a plain numeric value is a WTA ranking. Values such as
                // "ITF 285", "WTN 17.08", and "JE (-)" use incompatible scales.
                if (!/^\d+(?:\.\d+)?$/.test(text)) return 2000;
                const value = parseFloat(text);
                return Number.isFinite(value) && value > 0 ? value : 2000;
            }

            function entryDrawStrengthGM(players) {
                const ranks = (players || []).map(p => entryRankToNumber(p.rank)).filter(n => Number.isFinite(n) && n > 0);
                if (!ranks.length) return null;
                const logSum = ranks.reduce((acc, v) => acc + Math.log(v), 0);
                return Math.exp(logSum / ranks.length);
            }

            let _entryMenuGmMin = 0;
            let _entryMenuGmMax = 0;

            function styleEntryStrengthBadge(el, gm) {
                const gmEl = el ? el.querySelector('.entry-gm-value') : null;
                if (!gmEl) return;
                gmEl.style.background = Number.isFinite(gm) ? entryMenuGmBadgeColor(gm, _entryMenuGmMin, _entryMenuGmMax) : '#94a3b8';
                gmEl.style.color = '#1a1a1a';
            }

            function setEntryDrawStrength(players, key = '', allPlayers = null, byPosFn = null) {
                const el = document.getElementById('entry-strength');
                if (!el) return;
                const isQual = String(key || '').includes('#qual');
                const isITF = !!key && !String(key).startsWith('http');
                let gmPlayers = players || [];
                if (isQual && (!gmPlayers || gmPlayers.length === 0)) {
                    const sorter = byPosFn || ((a, b) => (Number((a && a.pos_num) ?? 999) - Number((b && b.pos_num) ?? 999)) || String((a && a.name) || '').localeCompare(String((b && b.name) || '')));
                    gmPlayers = (allPlayers || []).filter(p => p && p.type === 'QUAL').sort(sorter);
                } else if (isITF) {
                    const sorter = byPosFn || ((a, b) => (Number((a && a.pos_num) ?? 999) - Number((b && b.pos_num) ?? 999)) || String((a && a.name) || '').localeCompare(String((b && b.name) || '')));
                    const qualPlayers = (allPlayers || []).filter(p => p && p.type === 'QUAL').sort(sorter);
                    gmPlayers = entryFillITFMainPlaceholders(gmPlayers, qualPlayers);
                }
                const gm = entryDrawStrengthGM(gmPlayers);
                el.innerHTML = '<span class="entry-gm-value">' + (gm ? gm.toFixed(1) : '-') + '</span>';
                styleEntryStrengthBadge(el, gm);
            }

            function entryMenuGmBadgeColor(gm, min, max) {
                if (!Number.isFinite(gm)) return '#94a3b8';
                if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return 'rgba(148,163,184,0.65)';
                const t = Math.max(0, Math.min(1, (gm - min) / (max - min)));
                if (t < 0.25) {
                    const p = t / 0.25;
                    const r = Math.round(34 + p * (234 - 34));
                    const g = Math.round(197 + p * (179 - 197));
                    const b = Math.round(94 + p * (8 - 94));
                    return `rgba(${r},${g},${b},0.72)`;
                }
                if (t < 0.5) {
                    const p = (t - 0.25) / 0.25;
                    const r = Math.round(234 + p * (239 - 234));
                    const g = Math.round(179 + p * (140 - 179));
                    const b = Math.round(8 + p * (16 - 8));
                    return `rgba(${r},${g},${b},0.72)`;
                }
                if (t < 0.75) {
                    const p = (t - 0.5) / 0.25;
                    const r = Math.round(239 + p * (239 - 239));
                    const g = Math.round(140 + p * (68 - 140));
                    const b = Math.round(16 + p * (68 - 16));
                    return `rgba(${r},${g},${b},0.72)`;
                }
                const p = (t - 0.75) / 0.25;
                const r = Math.round(239 + p * (220 - 239));
                const g = Math.round(68 + p * (38 - 68));
                const b = Math.round(68 + p * (38 - 68));
                return `rgba(${r},${g},${b},0.72)`;
            }

            function sortEntryMenuByCategoryThenGm(rows) {
                const gmByItem = new Map(rows.map(row => [row.item, row.gm]));
                document.querySelectorAll('#view-entrylists .entry-menu-week').forEach(weekHeader => {
                    const items = [];
                    let nextWeek = weekHeader.nextElementSibling;
                    while (nextWeek && nextWeek.classList.contains('entry-menu-item')) {
                        items.push(nextWeek);
                        nextWeek = nextWeek.nextElementSibling;
                    }
                    if (items.length < 2) return;

                    const categoryOrder = new Map();
                    const originalOrder = new Map();
                    const categoryFor = item => String(item.getAttribute('data-level') || '')
                        .replace(/\s+/g, '').toUpperCase();
                    items.forEach((item, index) => {
                        const category = categoryFor(item);
                        if (!categoryOrder.has(category)) categoryOrder.set(category, categoryOrder.size);
                        originalOrder.set(item, index);
                    });
                    items.sort((a, b) => {
                        const categoryDiff = categoryOrder.get(categoryFor(a)) - categoryOrder.get(categoryFor(b));
                        if (categoryDiff) return categoryDiff;
                        const gmA = gmByItem.get(a);
                        const gmB = gmByItem.get(b);
                        const hasGmA = Number.isFinite(gmA);
                        const hasGmB = Number.isFinite(gmB);
                        if (hasGmA !== hasGmB) return hasGmA ? -1 : 1;
                        if (hasGmA && gmA !== gmB) return gmA - gmB;
                        return originalOrder.get(a) - originalOrder.get(b);
                    });
                    items.forEach(item => weekHeader.parentNode.insertBefore(item, nextWeek));
                });
            }

            function balanceEntryMenuRows() {
                const balanceClasses = ['entry-row-size-1', 'entry-row-size-2', 'entry-row-size-3'];
                document.querySelectorAll('#view-entrylists .entry-menu-week').forEach(weekHeader => {
                    const items = [];
                    let nextItem = weekHeader.nextElementSibling;
                    while (nextItem && nextItem.classList.contains('entry-menu-item')) {
                        items.push(nextItem);
                        nextItem = nextItem.nextElementSibling;
                    }
                    items.forEach(item => item.classList.remove(...balanceClasses));
                    const count = items.length;
                    const remainder = count % 4;
                    if (remainder === 1 && count >= 5) {
                        items.slice(0, 2).forEach(item => item.classList.add('entry-row-size-2'));
                        items.slice(2, 5).forEach(item => item.classList.add('entry-row-size-3'));
                    } else if (remainder) {
                        items.slice(0, remainder).forEach(item => item.classList.add(`entry-row-size-${remainder}`));
                    }
                });
            }

            function updateEntryMenuLabels() {
                const items = Array.from(document.querySelectorAll('#view-entrylists .entry-menu-item'));
                const rows = [];
                items.forEach(item => {
                    const key = item.getAttribute('data-key') || '';
                    const players = tournamentData[key];
                    if (!players) return;
                    const isITF = !key.startsWith('http');
                    const qualPlayers = players.filter(p => p && p.type === 'QUAL').sort(entryByPos);
                    const displayMain = key.includes('#qual') ? qualPlayers : entryGetDisplayMain(players, isITF);
                    const gmPlayers = key.includes('#qual') ? displayMain : (isITF ? entryFillITFMainPlaceholders(displayMain, qualPlayers) : displayMain);
                    const gm = entryDrawStrengthGM(gmPlayers);
                    rows.push({ item, gm, gmEl: item.querySelector('.entry-menu-gm-value') });
                });
                const gmValues = rows.map(row => row.gm).filter(Number.isFinite);
                _entryMenuGmMin = gmValues.length ? Math.min(...gmValues) : 0;
                _entryMenuGmMax = gmValues.length ? Math.max(...gmValues) : 0;
                rows.forEach(({ gm, gmEl }) => {
                    if (!gmEl) return;
                    gmEl.textContent = Number.isFinite(gm) ? gm.toFixed(1) : '-';
                    gmEl.style.background = Number.isFinite(gm) ? entryMenuGmBadgeColor(gm, _entryMenuGmMin, _entryMenuGmMax) : '#94a3b8';
                    gmEl.style.color = '#1a1a1a';
                });
                sortEntryMenuByCategoryThenGm(rows);
                balanceEntryMenuRows();
                const headerGm = document.querySelector('#entry-strength');
                if (headerGm) {
                    const headerText = headerGm.querySelector('.entry-gm-value');
                    const current = headerText ? parseFloat(String(headerText.textContent || '').replace(/[^\d.]/g, '')) : NaN;
                    styleEntryStrengthBadge(headerGm, current);
                }
            }

            function isMobileEntryLists() {
                return window.matchMedia('(max-width: 900px)').matches;
            }

            function syncEntryMenuToggle() {
                const view = document.getElementById('view-entrylists');
                const btn = document.getElementById('btn-open-entry-menu');
                if (!view || !btn) return;
                const shouldShow = isMobileEntryLists() && view.classList.contains('entry-menu-collapsed');
                btn.hidden = !shouldShow;
            }

            function setEntryMenuCollapsed(collapsed) {
                const view = document.getElementById('view-entrylists');
                if (!view) return;
                view.classList.toggle('entry-menu-collapsed', !!collapsed);
                syncEntryMenuToggle();
            }

            function openEntryTournamentList() {
                setEntryMenuCollapsed(false);
            }

            window.addEventListener('resize', syncEntryMenuToggle);

            function selectEntryTournament(el) {
                document.querySelectorAll('#view-entrylists .entry-menu-item').forEach(item => item.classList.remove('active'));
                el.classList.add('active');
                if (isMobileEntryLists()) setEntryMenuCollapsed(true);
                const nameEl = el.querySelector('.entry-menu-name');
                updateEntryList(el.getAttribute('data-key'), nameEl ? nameEl.textContent : el.textContent);
            }




            let _prioFilterActive = false;

            function togglePrio1() {
                _prioFilterActive = !_prioFilterActive;
                document.getElementById('btn-prio1').textContent = _prioFilterActive ? 'Show All' : 'Show Prio 1';
                updateEntryList();
            }

            function renderRows(list, isMain, isITF, renumber, showSeed, seedMap = null) {
                const prioCell = p => isITF ? `<td class="entry-pr-col">${p.priority||''}</td>` : '';
                const seedCell = p => {
                    if (!showSeed) return '';
                    if (seedMap && seedMap.has(p)) return `<td class="entry-seed-col">${seedMap.get(p)}</td>`;
                    return `<td class="entry-seed-col">${Number.isInteger(p.seed) ? p.seed : '-'}</td>`;
                };
                let html = '';
                list.forEach((p, i) => {
                    const displayPos = renumber ? (i + 1) : p.pos;
                    const bold = isMain ? 'font-weight:bold;' : '';
                    const flag = (p.country && p.country !== '-') ? countryFlag(p.country, false) + ' ' : '';
                    const nameDisplay = p.name.startsWith('(') ? p.name : getDisplayName(p.name.toUpperCase());
                    html += `<tr><td class="entry-pos-col">${displayPos}</td><td class="entry-player-col" style="text-align:left;${bold}">${flag}${nameDisplay}</td>${seedCell(p)}<td class="entry-rank-col">${p.rank}</td>${prioCell(p)}</tr>`;
                });
                return html;
            }

            function updateEntryList(key, name) {
                if (!key) {
                    const active = document.querySelector('.entry-menu-item.active');
                    if (!active) return;
                    key = active.getAttribute('data-key');
                    name = entryMenuNameForItem(active);
                }
                const body = document.getElementById('entry-body');
                document.getElementById('entry-title').textContent = name || 'Entry List';
                if (!tournamentData[key]) return;
                const players = tournamentData[key];
                const isITF = !key.startsWith('http');
                document.getElementById('entry-prio-header').style.display = isITF ? '' : 'none';
                const btn = document.getElementById('btn-prio1');
                btn.hidden = !isITF;
                if (!isITF) _prioFilterActive = false;
                btn.textContent = _prioFilterActive ? 'Show All' : 'Show Prio 1';
                const showSeed = players.some(p => Number.isInteger(p.seed));
                document.getElementById('entry-seed-header').style.display = showSeed ? '' : 'none';
                let html = '';
                const rankScore = p => {
                    const r = String(p.rank || '');
                    const m = r.match(/\d+(\.\d+)?/);
                    const n = m ? parseFloat(m[0]) : 9999;
                    if (r.startsWith('WTN')) return [2, n];
                    if (r.startsWith('ITF')) return [1, n];
                    return [0, n];
                };
                const byRank = (a, b) => {
                    const [ta, na] = rankScore(a);
                    const [tb, nb] = rankScore(b);
                    return (ta - tb) || (na - nb) || String(a.name || '').localeCompare(String(b.name || ''));
                };
                const byPos = (a, b) => (Number(a.pos_num ?? 999) - Number(b.pos_num ?? 999))
                    || String(a.name || '').localeCompare(String(b.name || ''));
                const sortEL = list => {
                    const mdo = list.filter(p => p.pos === 'MDO').sort(byRank);
                    const numbered = list.filter(p => p.pos !== 'MDO').sort(byPos);
                    if (mdo.length === 0) return numbered;
                    const used = new Set(numbered.map(p => p.pos_num));
                    const maxPos = numbered.length > 0 ? Math.max(...numbered.map(p => p.pos_num)) : 0;
                    const gaps = [];
                    for (let i = 1; i <= maxPos; i++) { if (!used.has(i)) gaps.push(i); }
                    const result = [];
                    let mi = 0, gi = 0, overflow = 1;
                    for (const p of numbered) {
                        while (mi < mdo.length && byRank(mdo[mi], p) < 0) {
                            const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                            result.push({...mdo[mi++], pos_num});
                        }
                        result.push(p);
                    }
                    while (mi < mdo.length) {
                        const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                        result.push({...mdo[mi++], pos_num});
                    }
                    return result;
                };
                const main = players.filter(p => p.type === 'MAIN').sort(byPos);
                const qual = sortEL(players.filter(p => p.type === 'QUAL'));
                const alt = sortEL(players.filter(p => p.type === 'ALT'));
                const cols = (isITF ? 5 : 4) + (showSeed ? 1 : 0);
                const qualifyingDivider = key.endsWith('#qual')
                    ? ''
                    : `<tr class="divider-row"><td colspan="${cols}">QUALIFYING</td></tr>`;
                let displayMain = main;
                let displayQual = qual;
                let displayAlt = alt;
                let prioSeedMap = null;

                if (_prioFilterActive) {
                    // JR prio1 players go at the bottom; non-prio1 JR spots filled from qual/alt
                    const mainJRPrio1 = main.filter(p => (p.entry === 'JR' || p.entry === 'JA') && p.priority === '1');
                    const mainRegular = main.filter(p => p.entry !== 'JR' && p.entry !== 'JA');
                    const regularSpots = main.length - mainJRPrio1.length;
                    const pool = [
                        ...mainRegular.filter(p => p.priority === '1'),
                        ...qual.filter(p => p.priority === '1'),
                        ...alt.filter(p => p.priority === '1'),
                    ];
                    displayMain = [
                        ...pool.slice(0, regularSpots),
                        ...mainJRPrio1,
                    ];
                    const seedSlots = Math.max(0, ...main
                        .map(p => Number.isInteger(p.seed) ? p.seed : 0));
                    const seedRankValue = p => {
                        const n = Number(p.seed_rank);
                        return Number.isFinite(n) && n > 0 ? n : null;
                    };
                    const bySeedRank = (a, b) => {
                        const sa = Number.isInteger(a.seed) ? a.seed : 9999;
                        const sb = Number.isInteger(b.seed) ? b.seed : 9999;
                        if (sa !== sb) return sa - sb;
                        const ra = seedRankValue(a);
                        const rb = seedRankValue(b);
                        if (ra !== null || rb !== null) {
                            return ((ra ?? 9999) - (rb ?? 9999)) || byRank(a, b);
                        }
                        return byRank(a, b);
                    };
                    const prioSeedCandidates = displayMain
                        .filter(p => !(String(p.name || '').startsWith('(')))
                        .sort(bySeedRank)
                        .slice(0, seedSlots);
                    prioSeedMap = new Map(prioSeedCandidates.map((p, i) => [p, i + 1]));
                    const remainingPool = pool.slice(regularSpots);
                    displayQual = remainingPool.slice(0, qual.length);
                    displayAlt  = remainingPool.slice(qual.length);
                    html += renderRows(displayMain, true, isITF, true, showSeed, prioSeedMap);
                    if (displayQual.length > 0) {
                        html += qualifyingDivider;
                        html += renderRows(displayQual, false, isITF, true, showSeed);
                    }
                    if (displayAlt.length > 0) {
                        html += `<tr class="divider-row"><td colspan="${cols}">ALTERNATES</td></tr>`;
                        html += renderRows(displayAlt, false, isITF, true, showSeed);
                    }
                } else {
                    html += renderRows(displayMain, true, isITF, false, showSeed);
                    if (qual.length > 0) {
                        html += qualifyingDivider;
                        html += renderRows(qual, false, isITF, false, showSeed);
                    }
                    if (alt.length > 0) {
                        html += `<tr class="divider-row"><td colspan="${cols}">ALTERNATES</td></tr>`;
                        html += renderRows(alt, false, isITF, false, showSeed);
                    }
                }
                body.innerHTML = html;
                setEntryDrawStrength(displayMain, key, players, byPos);
                updateEntryMenuLabels();
                syncEntryMenuToggle();
                syncUrlStateForTab('entrylists');
            }


            function renderHistoryTable() {
                const thead = document.getElementById('history-head');
                const tbody = document.getElementById('history-body');
                if (!thead || !tbody || _historyTableInitialized) return;

                // Define column headers (excluding hidden _ columns)
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'RND', 'PLAYER', 'SCORE', 'OPPONENT'];
                let headHtml = '<tr>';
                displayColumns.forEach(col => {
                    const headerText = col.replace('_', ' ');
                    headHtml += `<th>${headerText}</th>`;
                });
                headHtml += '</tr>';
                thead.innerHTML = headHtml;

                // Set initial placeholder message
                tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-info">Select a player to view their matches</td></tr>`;
                _historyTableInitialized = true;
            }

            let currentPlayerData = [];
            const HISTORY_SUBPAGE_MATCH = 'match';
            const HISTORY_SUBPAGE_MILESTONES = 'milestones';
            let historySubpage = HISTORY_SUBPAGE_MATCH;

            let _historyTableInitialized = false;
            let _milestonesTableInitialized = false;
            let _historyPlayerUniverse = null;
            let _historyPlayerUniverseUpper = null;
            let _milestonesIndex = null;
            let _milestonesIndexPromise = null;
            let _milestonesRenderSeq = 0;
            let _milestonesCategoryDefs = null;

            function renderMilestonesTable() {
                const thead = document.querySelector('#milestones-table thead');
                const tbody = document.getElementById('milestones-body');
                if (!thead || !tbody || _milestonesTableInitialized) return;

                thead.innerHTML = '<tr><th>PLAYER</th><th>WINS</th></tr>';
                tbody.innerHTML = '<tr><td colspan="2" class="cell-state-info">Open Milestones to view the leaderboard</td></tr>';
                _milestonesTableInitialized = true;
            }

            function _getMilestonesCategorySortRank(label) {
                const priority = [
                    'GS',
                    'WTA 1000 / P5 / PM',
                    'WTA 500 / P700',
                    'WTA 250 / International',
                    'WTA 125 / 125K Series',
                    'ITF',
                    'BJKC/Fed Cup',
                    'Olympic Games',
                    'Tier I',
                    'Tier II',
                    'Tier III',
                    'Tier IV',
                    'Tier V',
                    'Tier 2',
                    'Tier',
                    'WTA 1000',
                    'WTA 500',
                    'WTA 250',
                    'WTA 125',
                    '125K',
                    '125K Series',
                    'Premier Mandatory',
                    'Premier 5',
                    'Premier 700',
                    'Premier',
                    'International',
                    'International Gold',
                    'WTA',
                    'World Tour',
                    'WT',
                    'WTA Tour Championships',
                    'YE Championships'
                ];
                const idx = priority.indexOf(label);
                return idx >= 0 ? idx : 1000;
            }

            function _sortMilestonesCategoryLabels(labels) {
                return Array.from(labels).sort((a, b) => {
                    const rankA = _getMilestonesCategorySortRank(a);
                    const rankB = _getMilestonesCategorySortRank(b);
                    if (rankA !== rankB) return rankA - rankB;
                    return a.localeCompare(b);
                });
            }

            function getMilestonesCategoryDisplayLabel(label) {
                if (label === 'GS') return 'Grand Slams';
                return label;
            }

            function getMilestonesCategoryGroup(row) {
                const rawCategory = (row['CATEGORY'] || row['tournamentCategory'] || '').toString().trim();
                const matchType = getRowMatchType(row).toString().trim();
                const tournament = (row['TOURNAMENT'] || row['tournamentName'] || '').toString().trim();
                const categoryUpper = rawCategory.toUpperCase();
                const matchTypeUpper = matchType.toUpperCase();
                const tournamentUpper = tournament.toUpperCase();
                // Keep short codes exact so tournament names like Oegstgeest or Bogota do not false-match.
                const isExact = (...values) => values.some(value => categoryUpper === value || matchTypeUpper === value);
                const tournamentHas = (...values) => values.some(value => tournamentUpper.includes(value));
                const grandSlamNames = ['AUSTRALIAN OPEN', 'ROLAND GARROS', 'WIMBLEDON', 'US OPEN'];

                if (isExact('FED/BJK CUP') || tournamentHas('FED CUP', 'BJK CUP', 'BILLIE JEAN KING CUP', 'BJKC')) return 'BJKC/Fed Cup';
                if (isExact('OG') || tournamentHas('OLYMPIC')) return 'Olympic Games';
                if (isExact('GS') || categoryUpper.includes('GRAND SLAM') || tournamentHas('GRAND SLAM') || grandSlamNames.includes(tournamentUpper)) return 'GS';
                if (matchTypeUpper === 'ITF' || categoryUpper === 'ITF' || /^W\d+$/.test(categoryUpper) || tournamentUpper.includes('ITF')) return 'ITF';
                if (isExact('WTA 1000', 'PREMIER MANDATORY', 'PREMIER 5')) return 'WTA 1000 / P5 / PM';
                if (isExact('WTA 500', 'PREMIER 700', 'PREMIER')) return 'WTA 500 / P700';
                if (isExact('WTA 250', 'INTERNATIONAL', 'INTERNATIONAL GOLD')) return 'WTA 250 / International';
                if (isExact('WTA 125', '125K', '125K SERIES')) return 'WTA 125 / 125K Series';
                if (!rawCategory) {
                    if (matchTypeUpper === 'WTA') return 'WTA';
                    if (matchTypeUpper === 'OG') return 'Olympic Games';
                    if (matchTypeUpper === 'FED/BJK CUP') return 'BJKC/Fed Cup';
                }
                if (categoryUpper === 'TIER IIIV') return 'Tier III';
                return rawCategory;
            }

            function getMilestonesCategoryDefs() {
                if (!_milestonesIndex) return [];
                if (_milestonesCategoryDefs) return _milestonesCategoryDefs;
                const labels = new Set();
                _milestonesIndex.forEach(stat => {
                    if (!stat || !stat.active || !stat.playedCategories) return;
                    stat.playedCategories.forEach(label => {
                        if (label) labels.add(label);
                    });
                });
                _milestonesCategoryDefs = _sortMilestonesCategoryLabels(labels).map((label, idx) => ({
                    id: `milestones-filter-${idx}`,
                    key: label,
                    label: getMilestonesCategoryDisplayLabel(label)
                }));
                return _milestonesCategoryDefs;
            }

            function renderMilestonesFilters() {
                const body = document.getElementById('milestones-filter-body');
                if (!body) return;
                const defs = getMilestonesCategoryDefs();
                if (body.children.length) return;
                const filterHtml = defs.map(def => (
                    `<label class="milestones-filter-chip" for="${def.id}">
                        <input type="checkbox" id="${def.id}" checked>
                        <span>${escapeHtml(def.label)}</span>
                    </label>`
                )).join('');
                body.innerHTML = `${filterHtml}<label class="milestones-filter-chip" for="milestones-filter-qualy"><input type="checkbox" id="milestones-filter-qualy" checked><span>Include Qualy</span></label>`;
            }

            function getHistoryPlayerUniverse() {
                if (_historyPlayerUniverse && _historyPlayerUniverseUpper) return _historyPlayerUniverse;
                const select = document.getElementById('playerHistorySelect');
                const names = [];
                const upper = new Set();
                if (select && select.options) {
                    Array.from(select.options).forEach(option => {
                        const value = (option.value || '').toString().trim();
                        if (!value || value === '__ALL__' || value === 'Select Player...') return;
                        const upperValue = value.toUpperCase();
                        if (upper.has(upperValue)) return;
                        upper.add(upperValue);
                        names.push(value);
                    });
                }
                _historyPlayerUniverse = names;
                _historyPlayerUniverseUpper = upper;
                return names;
            }

            function isHistoryPlayerName(name) {
                if (!name) return false;
                if (!_historyPlayerUniverseUpper) getHistoryPlayerUniverse();
                return !!_historyPlayerUniverseUpper && _historyPlayerUniverseUpper.has(name.toString().toUpperCase());
            }

            function isWalkoverOrByeHistoryRow(row) {
                const statusDesc = (row['_resultStatusDesc'] || '').toString().toLowerCase();
                const scoreText = (row['SCORE'] || '').toString().toLowerCase();
                return statusDesc.includes('walkover') || statusDesc.includes('bye') || scoreText.includes('w/o') || scoreText === '-';
            }

            function isMilestonesQualifyingRow(row) {
                const draw = (row['DRAW'] || '').toString().trim().toUpperCase();
                const round = (row['ROUND'] || '').toString().trim().toUpperCase();
                return draw === 'Q' || draw.includes('QUAL') || round === 'Q' || /^Q\d+$/.test(round) || round.startsWith('QR');
            }

            async function ensureMilestonesIndex() {
                if (_milestonesIndex) return _milestonesIndex;
                if (!_milestonesIndexPromise) {
                    _milestonesIndexPromise = (async function() {
                        await ensureHistoryDataLoaded();
                        _milestonesIndex = buildMilestonesIndex();
                        return _milestonesIndex;
                    })();
                }
                return _milestonesIndexPromise;
            }

            function buildMilestonesIndex() {
                const universe = getHistoryPlayerUniverse();
                const playerByUpper = new Map(universe.map(name => [name.toUpperCase(), name]));
                const stats = new Map();
                const recentCutoff = new Date();
                recentCutoff.setFullYear(recentCutoff.getFullYear() - 2);
                recentCutoff.setHours(0, 0, 0, 0);

                function createStats(name) {
                    return {
                        name,
                        lastPlayed: null,
                        active: false,
                        wins: {},
                        playedCategories: new Set()
                    };
                }

                function getStats(name) {
                    if (!stats.has(name)) stats.set(name, createStats(name));
                    return stats.get(name);
                }

                function touchActive(entry, date) {
                    const ts = date.getTime();
                    if (entry.lastPlayed === null || ts > entry.lastPlayed) entry.lastPlayed = ts;
                    if (date >= recentCutoff) entry.active = true;
                }

                (Array.isArray(historyData) ? historyData : []).forEach(row => {
                    if (isWalkoverOrByeHistoryRow(row)) return;
                    const rowDate = new Date(row['DATE'] || '');
                    if (isNaN(rowDate)) return;

                    const winnerName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase());
                    const loserName = getDisplayName((row['_loserName'] || '').toString().toUpperCase());
                    const winnerKey = winnerName ? winnerName.toUpperCase() : '';
                    const loserKey = loserName ? loserName.toUpperCase() : '';
                    const winner = winnerKey ? playerByUpper.get(winnerKey) : '';
                    const loser = loserKey ? playerByUpper.get(loserKey) : '';

                    if (winner) touchActive(getStats(winner), rowDate);
                    if (loser) touchActive(getStats(loser), rowDate);

                    const categoryGroup = getMilestonesCategoryGroup(row);
                    if (categoryGroup) {
                        if (winner) getStats(winner).playedCategories.add(categoryGroup);
                        if (loser) getStats(loser).playedCategories.add(categoryGroup);
                    }
                    if (!categoryGroup || !winner) return;
                    const stat = getStats(winner);
                    if (!stat.wins[categoryGroup]) {
                        stat.wins[categoryGroup] = { main: 0, qualy: 0 };
                    }
                    if (isMilestonesQualifyingRow(row)) {
                        stat.wins[categoryGroup].qualy += 1;
                    } else {
                        stat.wins[categoryGroup].main += 1;
                    }
                });

                universe.forEach(name => {
                    if (!stats.has(name)) stats.set(name, createStats(name));
                });

                return stats;
            }

            function getMilestonesFilterState() {
                const defs = getMilestonesCategoryDefs();
                return {
                    categories: defs.filter(def => {
                        const el = document.getElementById(def.id);
                        return el ? el.checked : false;
                    }).map(def => def.key),
                    includeQualy: !!(document.getElementById('milestones-filter-qualy') && document.getElementById('milestones-filter-qualy').checked)
                };
            }

            function updateMilestonesCounter(count) {
                const counter = document.getElementById('milestones-active-counter');
                if (!counter) return;
                counter.textContent = '';
            }

            async function renderMilestonesPage() {
                const tbody = document.getElementById('milestones-body');
                if (!tbody) return;
                renderMilestonesTable();
                const renderSeq = ++_milestonesRenderSeq;
                tbody.innerHTML = '<tr><td colspan="2" class="cell-state-info">Loading milestones...</td></tr>';
                try {
                    await ensureMilestonesIndex();
                    renderMilestonesFilters();
                } catch (err) {
                    console.error('Failed to load milestones data:', err);
                    if (renderSeq !== _milestonesRenderSeq) return;
                    tbody.innerHTML = '<tr><td colspan="2" class="cell-state-error">Failed to load milestones. Please refresh and try again.</td></tr>';
                    updateMilestonesCounter(0);
                    return;
                }

                if (renderSeq !== _milestonesRenderSeq) return;
                renderMilestonesFilters();
                const selection = getMilestonesFilterState();
                const activePlayers = [];
                const categorySet = new Set(selection.categories);
                const stats = _milestonesIndex || new Map();

                stats.forEach(stat => {
                    if (!stat.active) return;
                    let totalWins = 0;
                    categorySet.forEach(category => {
                        const bucket = stat.wins[category];
                        if (!bucket) return;
                        totalWins += bucket.main + (selection.includeQualy ? bucket.qualy : 0);
                    });
                    if (totalWins <= 0) return;
                    activePlayers.push({
                        name: stat.name,
                        wins: totalWins,
                        lastPlayed: stat.lastPlayed || 0
                    });
                });

                activePlayers.sort((a, b) => {
                    if (b.wins !== a.wins) return b.wins - a.wins;
                    if (b.lastPlayed !== a.lastPlayed) return b.lastPlayed - a.lastPlayed;
                    return a.name.localeCompare(b.name);
                });

                updateMilestonesCounter(activePlayers.length);

                if (activePlayers.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="2" class="cell-state-error">No players found for the selected filters.</td></tr>';
                    return;
                }

                tbody.innerHTML = activePlayers.map(player => (
                    `<tr><td>${escapeHtml(player.name)}</td><td>${player.wins}</td></tr>`
                )).join('');
            }

            function applyMilestonesFilters() {
                renderMilestonesPage().then(() => syncUrlStateForTab('history'));
            }

            const milestonesFilterBody = document.getElementById('milestones-filter-body');
            if (milestonesFilterBody) {
                milestonesFilterBody.addEventListener('change', function(event) {
                    if (!event.target.matches('input[type="checkbox"]')) return;
                    applyMilestonesFilters();
                });
            }

            function syncHistorySubpageVisibility() {
                const historyLayout = document.querySelector('#view-history .history-layout');
                const filterPanel = historyLayout ? historyLayout.querySelector('.filter-panel') : null;
                const matchPage = document.getElementById('history-match-page');
                const milestonesPage = document.getElementById('history-milestones-page');
                if (filterPanel) filterPanel.style.display = historySubpage === HISTORY_SUBPAGE_MATCH ? '' : 'none';
                if (matchPage) matchPage.style.display = historySubpage === HISTORY_SUBPAGE_MATCH ? 'flex' : 'none';
                if (milestonesPage) milestonesPage.style.display = historySubpage === HISTORY_SUBPAGE_MILESTONES ? 'flex' : 'none';
            }

            function setHistorySubpage(page) {
                historySubpage = page === HISTORY_SUBPAGE_MILESTONES ? HISTORY_SUBPAGE_MILESTONES : HISTORY_SUBPAGE_MATCH;
                if (historySubpage !== HISTORY_SUBPAGE_MATCH) closeHistoryFilters(false);
                syncHistorySubpageVisibility();
                if (historySubpage === HISTORY_SUBPAGE_MILESTONES) {
                    renderMilestonesPage();
                } else {
                    applyMobileHistoryLayout();
                    applyHistoryFilters();
                }
                syncUrlStateForTab('history');
            }

            function syncFilterGroupState(group) {
                if (!group) return;
                const title = group.querySelector('.filter-group-title');
                if (title) {
                    title.setAttribute('aria-expanded', group.classList.contains('collapsed') ? 'false' : 'true');
                }
            }

            function toggleFilterGroup(element) {
                const group = element.closest('.filter-group');
                if (!group) return;
                group.classList.toggle('collapsed');
                syncFilterGroupState(group);
            }

            function toggleRankFilterGroup(element) {
                const group = element.closest('.filter-group');
                if (!group) return;
                const row = group.closest('.rank-filter-last-row');
                if (row) {
                    row.querySelectorAll('.filter-group').forEach(g => {
                        if (g !== group) {
                            g.classList.add('collapsed');
                            syncFilterGroupState(g);
                        }
                    });
                }
                group.classList.toggle('collapsed');
                syncFilterGroupState(group);
            }

            function getRowMatchType(row) {
                const explicit = (row['MATCH_TYPE'] || row['matchType'] || '').toString().trim();
                if (explicit) return explicit;

                // Backward-compatible fallback for older rows without matchType.
                const tournament = (row['TOURNAMENT'] || '').toString();
                const isITF = tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                              tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                              tournament.includes('W75') || tournament.includes('W100');
                return isITF ? 'ITF' : 'WTA';
            }

            function getRowYear(row) {
                const dateStr = (row['DATE'] || '').toString().trim();
                const match = dateStr.match(/^(\d{4})/);
                return match ? match[1] : '';
            }

            function getResultLabel(row, isWinner) {
                const statusDesc = (row['_resultStatusDesc'] || '').toString().toLowerCase();
                const scoreText = (row['SCORE'] || '').toString().toLowerCase();
                const isRet = statusDesc.includes('retired') || statusDesc.includes('ret.') || scoreText.includes('ret.');
                const isDef = statusDesc.includes('default') || statusDesc.includes('def.') || scoreText.includes('def.');

                if (isWinner) {
                    if (isRet) return 'Wins by RET';
                    if (isDef) return 'Wins by DEF';
                    return 'Wins';
                }
                if (isRet) return 'Losses by RET';
                if (isDef) return 'Losses by DEF';
                return 'Losses';
            }

            function isTeamEventRow(row) {
                const matchType = (row['MATCH_TYPE'] || row['matchType'] || '').toString();
                const category = (row['CATEGORY'] || row['tournamentCategory'] || '').toString();
                const tournament = (row['TOURNAMENT'] || row['tournamentName'] || '').toString();
                return (
                    matchType === 'Fed/BJK Cup' ||
                    category.includes('Fed/BJK Cup') ||
                    tournament.includes('BJK') ||
                    tournament.includes('Fed Cup')
                );
            }

            function isDoublesHistoryRow(row) {
                const wName = (row['_winnerName'] || '').toString();
                const lName = (row['_loserName'] || '').toString();
                const playerName = (row['PLAYER'] || '').toString();
                const opponentName = (row['OPPONENT'] || '').toString();
                return (
                    wName.includes('/') ||
                    lName.includes('/') ||
                    playerName.includes('/') ||
                    opponentName.includes('/')
                );
            }

            function getHistoryPerspective(row, selectedPlayer) {
                const winnerNameRaw = (row['_winnerName'] || "").toString().toUpperCase();
                const loserNameRaw = (row['_loserName'] || "").toString().toUpperCase();
                const winnerNameNormalized = getDisplayName(winnerNameRaw).toUpperCase();
                const loserNameNormalized = getDisplayName(loserNameRaw).toUpperCase();
                const winnerCountry = (row['_winnerCountry'] || '').toString().trim().toUpperCase();
                const loserCountry = (row['_loserCountry'] || '').toString().trim().toUpperCase();

                // Always keep the ARG side in PLAYER when only one side is ARG.
                if (winnerCountry === 'ARG' && loserCountry !== 'ARG') {
                    return {
                        isWinner: true,
                        winnerNameRaw,
                        loserNameRaw,
                        winnerNameNormalized,
                        loserNameNormalized
                    };
                }
                if (loserCountry === 'ARG' && winnerCountry !== 'ARG') {
                    return {
                        isWinner: false,
                        winnerNameRaw,
                        loserNameRaw,
                        winnerNameNormalized,
                        loserNameNormalized
                    };
                }

                // If both are ARG (or neither), preserve selected-player perspective when possible.
                if (selectedPlayer && selectedPlayer !== '__ALL__') {
                    if (winnerNameNormalized === selectedPlayer) {
                        return {
                            isWinner: true,
                            winnerNameRaw,
                            loserNameRaw,
                            winnerNameNormalized,
                            loserNameNormalized
                        };
                    }
                    if (loserNameNormalized === selectedPlayer) {
                        return {
                            isWinner: false,
                            winnerNameRaw,
                            loserNameRaw,
                            winnerNameNormalized,
                            loserNameNormalized
                        };
                    }
                }

                return {
                    isWinner: true,
                    winnerNameRaw,
                    loserNameRaw,
                    winnerNameNormalized,
                    loserNameNormalized
                };
            }

            function _formatTournName(name, category) {
                if (!name) return '';
                if (name.toUpperCase().includes('MALLORCA')) return 'WTA 125 Mallorca';
                const displayCategory = category && category.toUpperCase() === 'WT' ? 'World Tour' : category;
                const sep = name.lastIndexOf(' - ');
                if (sep === -1) return name;
                let city = name.slice(sep + 3);
                const comma = city.indexOf(',');
                if (comma !== -1) city = city.slice(0, comma).trim();
                return displayCategory ? displayCategory + ' ' + city : city;
            }

            function getRoundFilterLabel(row) {
                const roundValue = (row['ROUND'] || '').toString().trim();
                if (!roundValue) return '';
                const abbr = displayRound(roundValue, row['TOURNAMENT_ID'] || '', row['DATE'] || '',
                    row['TOURNAMENT'] || '', row['CATEGORY'] || '', row['MATCH_TYPE'] || '', row['DRAW'] || '');
                return isTeamEventRow(row) ? 'Team - ' + abbr : abbr;
            }

            function escapeHtml(value) {
                return String(value ?? '').replace(/[&<>"']/g, ch => ({
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;'
                }[ch]));
            }

            function normalizeHistoryFilterValues(values) {
                if (Array.isArray(values)) return values.filter(Boolean);
                return values ? [values] : [];
            }

            function mergeUniqueHistoryFilterValues(values, selectedValues) {
                const merged = [];
                const seen = new Set();
                [...normalizeHistoryFilterValues(values), ...normalizeHistoryFilterValues(selectedValues)].forEach(value => {
                    if (!value || seen.has(value)) return;
                    seen.add(value);
                    merged.push(value);
                });
                return merged;
            }

            function getHistoryFilterSelectionState() {
                const tournSelect = document.getElementById('filter-tournament-select');
                const oppSelect = document.getElementById('filter-opponent-select');
                const asRankInput = document.getElementById('filter-as-rank');
                const asRankModeEl = document.getElementById('filter-as-rank-mode');
                const vsRankInput = document.getElementById('filter-vs-rank');
                const vsRankModeEl = document.getElementById('filter-vs-rank-mode');

                return {
                    surfaces: getSelectedFilterValues('filter-surface'),
                    rounds: getSelectedFilterValues('filter-round'),
                    results: getSelectedFilterValues('filter-result'),
                    years: getSelectedFilterValues('filter-year'),
                    tournament: tournSelect ? tournSelect.value : '',
                    categories: getSelectedFilterValues('filter-category'),
                    opponent: oppSelect ? oppSelect.value : '',
                    opponentCountries: getSelectedFilterValues('filter-opponent-country'),
                    playerEntries: getSelectedFilterValues('filter-player-entry'),
                    seeds: getSelectedFilterValues('filter-seed'),
                    matchTypes: getSelectedFilterValues('filter-match-type'),
                    asRankVal: asRankInput && asRankInput.value ? parseInt(asRankInput.value, 10) : null,
                    asRankMode: asRankModeEl ? asRankModeEl.value : 'higher',
                    vsRankVal: vsRankInput && vsRankInput.value ? parseInt(vsRankInput.value, 10) : null,
                    vsRankMode: vsRankModeEl ? vsRankModeEl.value : 'higher'
                };
            }

            function rowMatchesHistoryFilters(row, filterState, selectedPlayer, excludedFilterKey = '') {
                if (isDoublesHistoryRow(row)) return false;

                const perspective = getHistoryPerspective(row, selectedPlayer);
                const isWinner = perspective.isWinner;
                const surface = row['SURFACE'] || '';
                const roundFilterLabel = getRoundFilterLabel(row);
                const resultLabel = getResultLabel(row, isWinner);
                const rowYear = getRowYear(row);
                const tournamentDisplay = _formatTournName(row['TOURNAMENT'] || '', row['CATEGORY'] || '');
                const rowCategory = row['CATEGORY'] || '';
                const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                const opponentDisplay = opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                const playerSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                const hasSeed = playerSeed ? 'Yes' : 'No';
                const matchType = getRowMatchType(row);

                if (excludedFilterKey !== 'surface' && filterState.surfaces.length > 0 && !filterState.surfaces.includes(surface)) return false;
                if (excludedFilterKey !== 'round' && filterState.rounds.length > 0 && !filterState.rounds.includes(roundFilterLabel)) return false;
                if (excludedFilterKey !== 'result' && filterState.results.length > 0 && !filterState.results.includes(resultLabel)) return false;
                if (excludedFilterKey !== 'year' && filterState.years.length > 0 && !filterState.years.includes('Career')) {
                    const wantLast52 = filterState.years.includes('Last 52');
                    const otherYears = filterState.years.filter(y => y !== 'Last 52');
                    let pass = false;
                    if (wantLast52) {
                        const today = new Date();
                        const dayOfWeek = today.getDay() === 0 ? 6 : today.getDay() - 1; // Mon=0
                        const weekStart = new Date(today);
                        weekStart.setDate(today.getDate() - dayOfWeek);
                        weekStart.setHours(0, 0, 0, 0);
                        const cutoff = new Date(weekStart);
                        cutoff.setDate(weekStart.getDate() - 51 * 7);
                        const rowDate = new Date(row['DATE'] || '');
                        if (!isNaN(rowDate) && rowDate >= cutoff) pass = true;
                    }
                    if (!pass && otherYears.length > 0 && otherYears.includes(rowYear)) pass = true;
                    if (!pass) return false;
                }
                if (excludedFilterKey !== 'tournament' && filterState.tournament && tournamentDisplay !== filterState.tournament) return false;
                if (excludedFilterKey !== 'category' && filterState.categories.length > 0 && !filterState.categories.includes(rowCategory)) return false;
                if (excludedFilterKey !== 'opponent' && filterState.opponent) {
                    if (opponentDisplay !== filterState.opponent) return false;
                }
                if (excludedFilterKey !== 'opponentCountry' && filterState.opponentCountries.length > 0 && !filterState.opponentCountries.includes(opponentCountry)) return false;
                if (excludedFilterKey !== 'playerEntry' && filterState.playerEntries.length > 0 && !filterState.playerEntries.includes(playerEntry)) return false;
                if (excludedFilterKey !== 'seed' && filterState.seeds.length > 0 && !filterState.seeds.includes(hasSeed)) return false;
                if (excludedFilterKey !== 'matchType' && filterState.matchTypes.length > 0 && !filterState.matchTypes.includes(matchType)) return false;
                if (excludedFilterKey !== 'asRank' && filterState.asRankVal !== null) {
                    const pr = parseInt(isWinner ? (row['_winnerRank'] || '') : (row['_loserRank'] || ''), 10);
                    if (isNaN(pr)) return false;
                    if (filterState.asRankMode === 'higher' && pr > filterState.asRankVal) return false;
                    if (filterState.asRankMode === 'lower' && pr < filterState.asRankVal) return false;
                }
                if (excludedFilterKey !== 'vsRank' && filterState.vsRankVal !== null) {
                    const vr = parseInt(isWinner ? (row['_loserRank'] || '') : (row['_winnerRank'] || ''), 10);
                    if (isNaN(vr)) return false;
                    if (filterState.vsRankMode === 'higher' && vr > filterState.vsRankVal) return false;
                    if (filterState.vsRankMode === 'lower' && vr < filterState.vsRankVal) return false;
                }

                return true;
            }

            function collectHistoryFilterValues(sourceRows, filterState, selectedPlayer, excludedFilterKey, valueGetter) {
                const values = new Set();
                (Array.isArray(sourceRows) ? sourceRows : []).forEach(row => {
                    if (!rowMatchesHistoryFilters(row, filterState, selectedPlayer, excludedFilterKey)) return;
                    const value = valueGetter(row);
                    if (value) values.add(value);
                });
                return Array.from(values);
            }

            function populateFilters(playerMatches, selectedState = null) {
                const selectionState = selectedState || getHistoryFilterSelectionState();
                const selectedPlayer = getNormalizedPlayerSelection('playerHistorySelect');

                const surfaces = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'surface', row => row['SURFACE'] || '');
                const rounds = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'round', row => getRoundFilterLabel(row));
                const resultsSet = new Set(collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'result', row => getResultLabel(row, getHistoryPerspective(row, selectedPlayer).isWinner)));
                const years = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'year', row => getRowYear(row));
                const tournaments = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'tournament', row => _formatTournName(row['TOURNAMENT'] || '', row['CATEGORY'] || ''));
                const categories = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'category', row => row['CATEGORY'] || '');
                const opponents = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'opponent', row => {
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const opponentName = perspective.isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    return opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                });
                const opponentCountries = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'opponentCountry', row => {
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    return perspective.isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                });
                const playerEntries = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'playerEntry', row => {
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    return perspective.isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                });
                const seeds = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'seed', row => {
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const playerSeed = perspective.isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    return playerSeed ? 'Yes' : 'No';
                });
                const matchTypes = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'matchType', row => getRowMatchType(row));

                // Populate filter options
                const orderedResults = [
                    'Wins',
                    'Losses',
                    'Wins by RET',
                    'Losses by RET',
                    'Wins by DEF',
                    'Losses by DEF'
                ].filter(r => resultsSet.has(r));
                const orderedYears = Array.from(new Set(years)).sort((a, b) => Number(b) - Number(a));
                orderedYears.unshift('Last 52');
                orderedYears.unshift('Career');

                populateFilterOptions('filter-surface', Array.from(new Set(surfaces)).sort((a, b) => a.localeCompare(b)), selectionState.surfaces);
                const roundOrderForFilter = {
                    'QR1': 1, 'QR2': 2, 'QR3': 3, 'QR4': 4,
                    'R128': 5, 'R64': 6, 'R32': 7, 'R16': 8,
                    'QF': 9, 'SF': 10, 'F': 11,
                    'Team - RR': 12, 'Team - R32': 13, 'Team - R16': 14,
                    'Team - QF': 15, 'Team - SF': 16, 'Team - F': 17,
                };
                const orderedRounds = Array.from(new Set(rounds)).sort((a, b) => {
                    const oa = roundOrderForFilter[a] ?? 99;
                    const ob = roundOrderForFilter[b] ?? 99;
                    return oa !== ob ? oa - ob : a.localeCompare(b);
                });
                populateFilterOptions('filter-round', orderedRounds, selectionState.rounds);
                populateFilterOptions('filter-result', orderedResults, selectionState.results);
                populateFilterOptions('filter-year', orderedYears, selectionState.years);
                populateTournamentSelect(Array.from(new Set(tournaments)).sort((a, b) => a.localeCompare(b)), selectionState.tournament);
                populateFilterOptions('filter-category', Array.from(new Set(categories)).sort((a, b) => a.localeCompare(b)), selectionState.categories);
                populateOpponentSelect(Array.from(new Set(opponents)).sort((a, b) => a.localeCompare(b)), selectionState.opponent);
                populateFilterOptions('filter-opponent-country', Array.from(new Set(opponentCountries)).sort((a, b) => a.localeCompare(b)), selectionState.opponentCountries);
                populateFilterOptions('filter-player-entry', Array.from(new Set(playerEntries)).sort((a, b) => a.localeCompare(b)), selectionState.playerEntries);
                populateFilterOptions('filter-seed', Array.from(new Set(seeds)), selectionState.seeds);
                populateFilterOptions('filter-match-type', Array.from(new Set(matchTypes)).sort((a, b) => a.localeCompare(b)), selectionState.matchTypes);
            }

            function populateFilterOptions(filterId, values, selectedValues = []) {
                const container = document.getElementById(filterId);
                if (!container) return;
                const selectedList = normalizeHistoryFilterValues(selectedValues);
                const selectedSet = new Set(selectedList);
                let html = '';
                mergeUniqueHistoryFilterValues(values, selectedList).forEach(value => {
                    if (value) {
                        const selectedClass = selectedSet.has(value) ? ' selected' : '';
                        const pressed = selectedSet.has(value) ? 'true' : 'false';
                        html += `<button type="button" class="filter-option${selectedClass}" data-value="${escapeHtml(value)}" aria-pressed="${pressed}">${escapeHtml(value)}</button>`;
                    }
                });
                container.innerHTML = html || '<div style="padding: 5px; color: #94a3b8; font-size: 11px;">No options</div>';
            }

            function populateTournamentSelect(tournaments, selectedTournament = '') {
                const select = document.getElementById('filter-tournament-select');
                if (!select) return;
                const selectedValue = selectedTournament || '';
                const optionValues = mergeUniqueHistoryFilterValues(tournaments, selectedValue).sort((a, b) => a.localeCompare(b));

                if ($(select).data('select2')) {
                    $(select).select2('destroy');
                }

                let html = '<option value="">All Tournaments</option>';
                optionValues.forEach(tournament => {
                    if (tournament) {
                        html += `<option value="${escapeHtml(tournament)}">${escapeHtml(tournament)}</option>`;
                    }
                });
                select.innerHTML = html;

                $(select).select2({
                    placeholder: 'All Tournaments',
                    allowClear: true,
                    width: '100%'
                });
                $(select).val(selectedValue).trigger('change.select2');

                $(select).off('change').on('change', function() {
                    const selectedText = this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : 'All Tournaments';
                    const rendered = this.nextElementSibling
                        ? this.nextElementSibling.querySelector('.select2-selection__rendered')
                        : null;
                    if (rendered) {
                        rendered.textContent = selectedText;
                        rendered.title = selectedText;
                    }
                    applyHistoryFilters();
                });
            }

            function populateOpponentSelect(opponents, selectedOpponent = '') {
                const select = document.getElementById('filter-opponent-select');
                if (!select) return;
                const selectedValue = selectedOpponent || '';
                const optionValues = mergeUniqueHistoryFilterValues(opponents, selectedValue).sort((a, b) => a.localeCompare(b));

                // Destroy existing Select2 if it exists
                if ($(select).data('select2')) {
                    $(select).select2('destroy');
                }

                // Clear and populate options
                let html = '<option value="">All Opponents</option>';
                optionValues.forEach(opponent => {
                    if (opponent) {
                        html += `<option value="${escapeHtml(opponent)}">${escapeHtml(opponent)}</option>`;
                    }
                });
                select.innerHTML = html;

                // Initialize Select2 with search
                $(select).select2({
                    placeholder: 'All Opponents',
                    allowClear: true,
                    width: '100%'
                });
                $(select).val(selectedValue).trigger('change.select2');

                // Auto-apply filters when selection changes
                $(select).off('change').on('change', function() {
                    const selectedText = this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : 'All Opponents';
                    const rendered = this.nextElementSibling
                        ? this.nextElementSibling.querySelector('.select2-selection__rendered')
                        : null;
                    if (rendered) {
                        rendered.textContent = selectedText;
                        rendered.title = selectedText;
                    }
                    applyHistoryFilters();
                });
            }

            function toggleFilterOption(event, element) {
                // Mobile taps are additive; desktop keeps Ctrl/Cmd+Click multi-select.
                const additiveSelection = event.ctrlKey || event.metaKey || window.innerWidth <= 768;
                if (!additiveSelection) {
                    // Plain desktop click - deselect all others in this group first
                    const siblings = element.parentElement.querySelectorAll('.filter-option');
                    siblings.forEach(sib => {
                        if (sib !== element) {
                            sib.classList.remove('selected');
                            sib.setAttribute('aria-pressed', 'false');
                        }
                    });
                }

                // Toggle this option
                element.classList.toggle('selected');
                element.setAttribute('aria-pressed', element.classList.contains('selected') ? 'true' : 'false');

                // Auto-apply filters
                applyHistoryFilters();
            }

            const historyFilterPanel = document.getElementById('history-filter-panel');
            if (historyFilterPanel) {
                historyFilterPanel.addEventListener('click', function(event) {
                    const element = event.target.closest ? event.target.closest('.filter-option') : null;
                    if (!element || !historyFilterPanel.contains(element)) return;
                    toggleFilterOption(event, element);
                });
            }

            function getSelectedFilterValues(filterId) {
                const container = document.getElementById(filterId);
                const selectedOptions = container.querySelectorAll('.filter-option.selected');
                return Array.from(selectedOptions).map(option => option.getAttribute('data-value'));
            }

            function updateHistoryCounter(matches, selectedPlayer) {
                const counter = document.getElementById('history-wl-counter');
                if (!counter) return;

                const nonWO = (matches || []).filter(row =>
                    !isDoublesHistoryRow(row) &&
                    !['Walkover', 'Bye'].includes(row['_resultStatusDesc'] || '')
                );
                const total = nonWO.length;
                if (!selectedPlayer || total === 0) {
                    counter.textContent = `Matches: ${total}`;
                    return;
                }
                if (selectedPlayer === '__ALL__') {
                    let wins = 0, argVsArg = 0;
                    nonWO.forEach(row => {
                        const wc = (row['_winnerCountry'] || '').toUpperCase();
                        const lc = (row['_loserCountry'] || '').toUpperCase();
                        if (wc === 'ARG' && lc === 'ARG') { argVsArg++; }
                        else if (wc === 'ARG') { wins++; }
                    });
                    const losses = total - wins - argVsArg;
                    const record = argVsArg > 0 ? `${wins}-${argVsArg}-${losses}` : `${wins}-${losses}`;
                    counter.textContent = `Matches: ${total} (${record})`;
                    return;
                }

                let wins = 0;
                nonWO.forEach(row => {
                    if (getHistoryPerspective(row, selectedPlayer).isWinner) wins += 1;
                });
                const losses = total - wins;
                counter.textContent = `Matches: ${total} (${wins}-${losses})`;
            }

            function applyHistoryFilters() {
                const selectedPlayer = getNormalizedPlayerSelection('playerHistorySelect');
                const filterState = getHistoryFilterSelectionState();
                updateHistoryMobileFilterButton(filterState);
                if (!selectedPlayer) {
                    syncUrlStateForTab('history');
                    return;
                }

                // Filter the data (if nothing selected in a category, show all)
                const filtered = currentPlayerData.filter(row => rowMatchesHistoryFilters(row, filterState, selectedPlayer));

                populateFilters(currentPlayerData, filterState);
                updateHistoryCounter(filtered, selectedPlayer);
                renderFilteredMatches(filtered, selectedPlayer);
                syncUrlStateForTab('history');
            }

            function clearHistoryFilters() {
                // Remove selected class from all filter options
                document.querySelectorAll('.filter-option.selected').forEach(option => {
                    option.classList.remove('selected');
                    option.setAttribute('aria-pressed', 'false');
                });
                $('#filter-tournament-select').val('').trigger('change');
                // Reset opponent select dropdown
                $('#filter-opponent-select').val('').trigger('change');
                // Reset rank filters
                const asRankInput = document.getElementById('filter-as-rank');
                const vsRankInput = document.getElementById('filter-vs-rank');
                const asRankMode = document.getElementById('filter-as-rank-mode');
                const vsRankMode = document.getElementById('filter-vs-rank-mode');
                if (asRankInput) asRankInput.value = '';
                if (vsRankInput) vsRankInput.value = '';
                if (asRankMode) asRankMode.value = 'higher';
                if (vsRankMode) vsRankMode.value = 'higher';
                // Auto-apply filters (which will show all matches since nothing is selected)
                applyHistoryFilters();
            }

            const HISTORY_PAGE_SIZE = 1000;
            let _historyPagedMatches = [];
            let _historyPagedPlayer = '';
            let _historyCurrentPage = 1;

            function renderFilteredMatches(matches, selectedPlayer) {
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'RND', 'PLAYER', 'SCORE', 'OPPONENT'];
                matches = (matches || []).filter(row => !isDoublesHistoryRow(row));
                updateHistoryCounter(matches, selectedPlayer);

                if (matches.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-error">No matches found with the selected filters.</td></tr>`;
                    _updateHistoryPagination(0, 1, 1);
                    return;
                }

                // Round priority (lower = higher in table)
                const roundOrder = {
                    'Final': 1, 'Semi-finals': 2, 'Quarter-finals': 3,
                    '4th Round': 4, '3rd Round': 5, '2nd Round': 6, '1st Round': 7,
                    'QR4': 8, 'QR3': 9, 'QR2': 10, 'QR1': 11,
                    'Semi Finals': 12, 'Quarter Finals': 13,
                    'Last 16': 14, 'Last 32': 15, 'Round Robin': 16
                };
                function getRoundOrder(round) {
                    return roundOrder[round] || 99;
                }

                // Sort by date descending, then by round order ascending
                matches.sort((a, b) => {
                    const dateA = formatDate(a['DATE'] || '1900-01-01');
                    const dateB = formatDate(b['DATE'] || '1900-01-01');
                    if (dateA !== dateB) return dateB.localeCompare(dateA);
                    return getRoundOrder(a['ROUND'] || '') - getRoundOrder(b['ROUND'] || '');
                });

                _historyPagedMatches = matches;
                _historyPagedPlayer = selectedPlayer;
                _renderHistoryPage(1);
            }

            function _renderHistoryPage(page) {
                const total = _historyPagedMatches.length;
                const totalPages = Math.ceil(total / HISTORY_PAGE_SIZE);
                _historyCurrentPage = Math.max(1, Math.min(page, totalPages));
                const start = (_historyCurrentPage - 1) * HISTORY_PAGE_SIZE;
                const pageMatches = _historyPagedMatches.slice(start, start + HISTORY_PAGE_SIZE);
                const selectedPlayer = _historyPagedPlayer;

                const parts = [];
                for (let i = 0; i < pageMatches.length; i++) {
                    const row = pageMatches[i];
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const isWinner = perspective.isWinner;
                    const playerNameRaw = isWinner ? perspective.winnerNameRaw : perspective.loserNameRaw;
                    const playerDisplayName = getDisplayName(playerNameRaw);

                    const rivalName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    const rivalDisplayName = rivalName ? getDisplayName(rivalName.toUpperCase()) : '';

                    const pSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const pEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    const rSeed = isWinner ? (row['_loserSeed'] || '') : (row['_winnerSeed'] || '');
                    const rEntry = isWinner ? (row['_loserEntry'] || '') : (row['_winnerEntry'] || '');

                    const playerRank = (isWinner ? (row['_winnerRank'] || '') : (row['_loserRank'] || '')).toString();
                    const oppRank = (isWinner ? (row['_loserRank'] || '') : (row['_winnerRank'] || '')).toString();
                    const rivalCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    const playerCell = buildHistoryPlayerCell(playerRank, 'ARG', pSeed, pEntry, playerDisplayName);
                    const opponentCell = buildHistoryPlayerCell(oppRank, rivalCountry, rSeed, rEntry, rivalDisplayName);
                    const scoreText = isWinner ? (row['SCORE'] || '') : reverseScore(row['SCORE'] || '');
                    const scoreClass = isWinner ? 'score-win' : 'score-loss';

                    const displayTournament = _formatTournName(row['TOURNAMENT'] || '', row['CATEGORY'] || '');

                    parts.push('<tr><td>', formatDate(row['DATE'] || ''),
                        '</td><td>', displayTournament,
                        '</td><td>', row['SURFACE'] || '',
                        '</td><td>', displayRound(row['ROUND'] || '', row['TOURNAMENT_ID'] || '', row['DATE'] || '', row['TOURNAMENT'] || '', row['CATEGORY'] || '', row['MATCH_TYPE'] || '', row['DRAW'] || ''),
                        '</td><td>', playerCell,
                        '</td><td class="', scoreClass, '">', `<span class="score-badge">${scoreText}</span>`,
                        '</td><td>', opponentCell,
                        '</td></tr>');
                }
                document.getElementById('history-body').innerHTML = parts.join('');
                _updateHistoryPagination(total, _historyCurrentPage, totalPages);
            }

            function _updateHistoryPagination(total, currentPage, totalPages) {
                const container = document.getElementById('history-pagination');
                if (!container) return;
                if (total <= HISTORY_PAGE_SIZE) {
                    container.style.display = 'none';
                    return;
                }
                const start = (currentPage - 1) * HISTORY_PAGE_SIZE + 1;
                const end = Math.min(currentPage * HISTORY_PAGE_SIZE, total);
                const prevDisabled = currentPage === 1 ? 'disabled' : '';
                const nextDisabled = currentPage === totalPages ? 'disabled' : '';
                container.style.display = 'flex';
                container.innerHTML =
                    `<button class="history-page-btn" ${prevDisabled} onclick="_renderHistoryPage(_historyCurrentPage - 1)">&#9664; Prev</button>` +
                    `<span>${start}-${end} of ${total}</span>` +
                    `<button class="history-page-btn" ${nextDisabled} onclick="_renderHistoryPage(_historyCurrentPage + 1)">Next &#9654;</button>`;
            }

            async function filterHistoryByPlayer() {
                const selectedPlayer = getNormalizedPlayerSelection('playerHistorySelect');
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'RND', 'PLAYER', 'SCORE', 'OPPONENT'];

                if (selectedPlayer === '__ALL__') {
                    tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-info">Loading match history...</td></tr>`;
                    try {
                        await ensureHistoryDataLoaded();
                    } catch (err) {
                        console.error('Failed to load match history:', err);
                        tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-error">Failed to load match history. Please refresh and try again.</td></tr>`;
                        updateHistoryCounter([], '__ALL__');
                        return;
                    }
                    const allFiltered = historyData.filter(row => !isDoublesHistoryRow(row));
                    if (allFiltered.length === 0) {
                        tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-error">No matches found.</td></tr>`;
                        updateHistoryCounter([], '__ALL__');
                        return;
                    }
                    currentPlayerData = allFiltered;
                    applyHistoryFilters();
                    return;
                }

                if (!selectedPlayer) {
                    currentPlayerData = [];
                    ['filter-surface', 'filter-round', 'filter-result', 'filter-year', 'filter-category', 'filter-opponent-country', 'filter-player-entry', 'filter-seed', 'filter-match-type']
                        .forEach(id => {
                            const el = document.getElementById(id);
                            if (el) el.innerHTML = '';
                        });
                    const tournSelect = document.getElementById('filter-tournament-select');
                    if (tournSelect) {
                        tournSelect.innerHTML = '<option value="">All Tournaments</option>';
                        if ($(tournSelect).data('select2')) {
                            $(tournSelect).select2('destroy');
                        }
                    }
                    const asRankInput = document.getElementById('filter-as-rank');
                    const vsRankInput = document.getElementById('filter-vs-rank');
                    const asRankMode = document.getElementById('filter-as-rank-mode');
                    const vsRankMode = document.getElementById('filter-vs-rank-mode');
                    if (asRankInput) asRankInput.value = '';
                    if (vsRankInput) vsRankInput.value = '';
                    if (asRankMode) asRankMode.value = 'higher';
                    if (vsRankMode) vsRankMode.value = 'higher';
                    const oppSelect = document.getElementById('filter-opponent-select');
                    if (oppSelect) {
                        if ($(oppSelect).data('select2')) {
                            $(oppSelect).select2('destroy');
                        }
                        oppSelect.innerHTML = '<option value="">All Opponents</option>';
                    }
                    tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-error">Select a player...</td></tr>`;
                    updateHistoryCounter([], '');
                    updateHistoryMobileFilterButton();
                    syncUrlStateForTab('history');
                    return;
                }

                tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-info">Loading match history...</td></tr>`;
                try {
                    await ensureHistoryDataLoaded();
                } catch (err) {
                    console.error('Failed to load match history:', err);
                    tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-error">Failed to load match history. Please refresh and try again.</td></tr>`;
                    updateHistoryCounter([], selectedPlayer);
                    return;
                }

                const filtered = historyData.filter(row => {
                    if (isDoublesHistoryRow(row)) return false;
                    // For a selected player, only keep rows where she is the rendered PLAYER side.
                    // This guarantees PLAYER remains the ARG-side view and nationality-switch rows
                    // where she appears only as OPPONENT are excluded (but still visible in ALL).
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const playerNameNormalized = perspective.isWinner
                        ? perspective.winnerNameNormalized
                        : perspective.loserNameNormalized;
                    return playerNameNormalized === selectedPlayer;
                });

                if (filtered.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="${displayColumns.length}" class="cell-state-error">No matches found for this player.</td></tr>`;
                    updateHistoryCounter([], selectedPlayer);
                    return;
                }

                // Store current player data for filtering
                currentPlayerData = filtered;

                applyHistoryFilters();
            }
