(function () {
    'use strict';

    const data = window.WTARG_DATA.planner || { events: [], players: [] };
    const events = data.events || [];
    const players = data.players || [];
    const byId = new Map(events.map(event => [event.id, event]));
    const state = {
        player: '', starting: '', ending: '',
        compare: [], schedule: [], finishes: {}, routes: {}, target: '', active: ''
    };
    let mounted = false;
    let restoreControlFocus = true;
    let breakdownExpanded = true;
    let notice = '';

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[character]));
    }
    function text(value) { return '<span>' + esc(value) + '</span>'; }
    function noTranslate(value) { return '<span translate="no">' + esc(value) + '</span>'; }
    function weekFor(event) {
        const week = event.week || event.startDate;
        const currentWeek = monday(data.asOf);
        return week && week < currentWeek && event.endDate >= currentWeek ? currentWeek : week;
    }
    function eventName(event) { return event.displayName || (event.name === event.level ? event.name : event.name.replace(/\s+\d+$/, '')); }
    function shortDate(value) {
        return new Date(value + 'T00:00:00Z').toLocaleDateString('en-US', {month: 'long', day: 'numeric', timeZone: 'UTC'});
    }
    function delayedPoints(event) {
        return ['W15', 'W35'].includes(event.level) && (data.w50CutoffWeeks || []).includes(weekFor(event));
    }
    function regionLabel(value) {
        const labels = (data.regions || {})[value];
        return labels ? '<span class="cal-cont-label-desktop">' + text(labels.label) + '</span>'
            + '<span class="cal-cont-label-mobile">' + text(labels.mobileLabel) + '</span>' : text(value || 'Region unknown');
    }
    function slug(value) {
        return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
            .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    }
    function day(value) {
        value = String(value || '').slice(0, 10);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return '—';
        return value.slice(8, 10) + '/' + value.slice(5, 7) + '/' + value.slice(0, 4);
    }
    function numeric(value, digits = 0) {
        return Number.isFinite(Number(value)) && value !== null && value !== ''
            ? Number(value).toLocaleString('en', { maximumFractionDigits: digits }) : '—';
    }
    function plus(value) { return (value > 0 ? '+' : '') + numeric(value); }
    function selectedPlayer() { return players.find(player => player.id === state.player); }
    function entryFor(event, playerId) {
        return (event.entries || []).find(entry => entry.playerKey === playerId);
    }
    function entryRoutes(event, playerId) {
        if (event.rest) return ['MAIN'];
        const progress = (event.progress || {})[playerId];
        if (progress) return [progress.route];
        const entry = entryFor(event, playerId);
        if (entry && entry.type === 'MAIN') return ['MAIN'];
        return ['MAIN', 'QUAL'];
    }
    function lockedWeeks(playerId) {
        const locked = new Map();
        for (const event of events) {
            const progress = (event.progress || {})[playerId];
            if (!progress || (event.endDate || event.startDate) < monday(data.asOf)) continue;
            const week = weekFor(event);
            const previous = byId.get(locked.get(week));
            const previousProgress = previous && previous.progress[playerId];
            if (!previous || (previousProgress.completed && !progress.completed)
                || (previousProgress.completed === progress.completed && event.startDate >= previous.startDate)) {
                locked.set(week, event.id);
            }
        }
        return locked;
    }
    function eligibleEvents(source, playerId, asOf) {
        const locked = lockedWeeks(playerId);
        return source.filter(event => (event.endDate || event.startDate) >= monday(asOf)
            && (!locked.has(weekFor(event)) || locked.get(weekFor(event)) === event.id)
            && entryRoutes(event, playerId).length > 0);
    }
    window.plannerEligibleEvents = eligibleEvents;
    window.plannerEntryRoutes = entryRoutes;
    const addDays = (value, days) => {
        const result = new Date(value + 'T00:00:00Z');
        result.setUTCDate(result.getUTCDate() + days);
        return result.toISOString().slice(0, 10);
    };
    function monday(value) {
        const date = new Date(value + 'T00:00:00Z');
        return addDays(value, -(date.getUTCDay() + 6) % 7);
    }
    function upcomingEvents() { return eligibleEvents(events, state.player, data.asOf); }
    function weekOptions() { return [...new Set([monday(data.asOf), ...upcomingEvents().map(weekFor)])].sort(); }
    function slamShortName(name) {
        return ({'Australian Open':'AO', 'Roland Garros':'RG', 'Wimbledon':'WMB', 'US Open':'USO'})[name] || name;
    }
    function slamOptions() {
        return (data.gsDeadlines || []).flatMap(gs => ['md', 'q']
            .filter(route => /^\d{4}-\d{2}-\d{2}$/.test(route === 'md' ? gs.mdCutoff : gs.qCutoff))
            .map(route => ({
            value: ('gs-' + gs.id + '-' + gs.year + '-' + route).toLowerCase(),
            label: slamShortName(gs.name) + "'" + String(gs.year).slice(-2)
                + (gs.mdCutoff === gs.qCutoff ? '' : route === 'md' ? ' MD' : ' Q')
                + ' · ' + day(monday(addDays(route === 'md' ? gs.mdCutoff : gs.qCutoff, -1))),
            deadlineLabel: slamShortName(gs.name) + "'" + String(gs.year).slice(-2)
                + (gs.mdCutoff === gs.qCutoff ? '' : route === 'md' ? ' MD' : ' Q'),
            week: monday(addDays(route === 'md' ? gs.mdCutoff : gs.qCutoff, -1)),
            date: route === 'md' ? gs.mdCutoff : gs.qCutoff
        }))).filter(option => option.date >= data.asOf).sort((a, b) => a.date.localeCompare(b.date));
    }
    function startingOptions() {
        const slams = slamOptions();
        return weekOptions().map(value => {
            const cutoff = slams.find(slam => slam.week === value);
            return {value, label: cutoff ? cutoff.label : day(value), deadlineLabel: cutoff ? cutoff.deadlineLabel : ''};
        });
    }
    function endingOptions() {
        const firstWeek = monday(data.asOf);
        const nextQualifying = (data.gsDeadlines || []).map(gs => gs.qCutoff)
            .filter(date => /^\d{4}-\d{2}-\d{2}$/.test(date) && date >= data.asOf).sort()[0];
        const lastWeek = [addDays(firstWeek, 140), nextQualifying ? monday(addDays(nextQualifying, -1)) : '', state.starting].sort().pop();
        const slams = slamOptions();
        const options = [];
        for (let week = state.starting || firstWeek; week <= lastWeek; week = addDays(week, 7)) {
            const cutoff = slams.find(slam => slam.week === week && monday(addDays(slam.date, -1)) >= (state.starting || firstWeek));
            options.push({value: week, label: cutoff ? cutoff.label : day(week),
                deadlineLabel: cutoff ? cutoff.deadlineLabel : '',
                date: cutoff ? cutoff.date : addDays(week, 7),
                lastWeek: cutoff ? monday(addDays(cutoff.date, -1)) : week});
        }
        return options;
    }
    function endingDate() {
        const selected = endingOptions().find(option => option.value === state.ending);
        return selected ? selected.date : state.ending ? addDays(state.ending, 7) : '';
    }
    function calendarEvents() {
        const rests = endingOptions().map(option => {
            const id = 'rest:' + option.value;
            if (!byId.has(id)) byId.set(id, {id, rest: true, name: 'Rest', level: 'REST', surface: 'Rest',
                continent: 'rest', week: option.value, startDate: option.value, endDate: addDays(option.value, 6)});
            return byId.get(id);
        });
        return eligibleEvents(events.concat(rests), state.player, data.asOf);
    }
    function endingWeek() {
        const selected = endingOptions().find(option => option.value === state.ending);
        return selected ? selected.lastWeek : state.ending;
    }
    window.plannerSlamOptions = slamOptions;
    function rangeIncludes(event) {
        return (!state.starting || weekFor(event) >= state.starting) && (!endingWeek() || weekFor(event) <= endingWeek());
    }
    function routeFor(event) {
        const routes = entryRoutes(event, state.player);
        return routes.includes(state.routes[event.id]) ? state.routes[event.id] : routes[0] || 'MAIN';
    }
    function pointEvent(event) {
        return Object.assign({}, event, { assumedRoute: routeFor(event), currentProgress: (event.progress || {})[state.player] });
    }
    function finishesFor(event) {
        if (event.rest) return [];
        return typeof window.plannerPointFinishes === 'function'
            ? window.plannerPointFinishes(pointEvent(event), routeFor(event)) : [];
    }
    function finishFor(event) {
        const finishes = finishesFor(event);
        if (finishes.some(finish => finish.value === state.finishes[event.id])) return state.finishes[event.id];
        const progress = (event.progress || {})[state.player];
        if (progress && finishes.some(finish => finish.value === progress.round)) return progress.round;
        return (finishes.find(finish => /^R\d+$/.test(finish.value)) || finishes[0] || {}).value || '';
    }
    function pointDate(event) {
        return typeof window.plannerPointDate === 'function'
            ? window.plannerPointDate(event) : (event.endDate || event.startDate);
    }
    function scenario(event, finish, horizon) {
        if (typeof window.plannerPointScenario !== 'function' || !selectedPlayer()) return null;
        if (!finishesFor(event).length) return null;
        return window.plannerPointScenario(selectedPlayer().name, pointEvent(event),
            finish || finishFor(event), horizon || endingDate() || pointDate(event));
    }
    function jointScenario(selected, horizon) {
        if (typeof window.plannerScheduleScenario !== 'function' || !selectedPlayer()) return null;
        return window.plannerScheduleScenario(selectedPlayer().name, selected.filter(event => !event.rest).map(event => ({
            event: pointEvent(event), finish: finishFor(event), route: routeFor(event)
        })), horizon);
    }
    function minimumFinish(event, horizon) {
        if (!finishesFor(event).length) return 'Point table unavailable';
        for (const finish of finishesFor(event)) {
            const result = scenario(event, finish.value, horizon);
            if (result && result.gain > 0) return finish.value;
        }
        return 'No countable gain';
    }
    function filteredEvents() {
        const chosen = new Map(state.schedule.map(id => [weekFor(byId.get(id)), id]));
        const categories = ['GS', 'Grand Slam', 'WTA 1000', 'WTA 500', 'WTA 250', 'WTA 125', 'W100', 'W75', 'W50', 'W35', 'W15'];
        const categoryRank = event => categories.includes(event.level) ? categories.indexOf(event.level) : categories.length;
        return calendarEvents().filter(event =>
            rangeIncludes(event)
            && (!chosen.has(weekFor(event)) || chosen.get(weekFor(event)) === event.id)
        ).sort((a, b) => weekFor(a).localeCompare(weekFor(b)) || categoryRank(a) - categoryRank(b) || eventName(a).localeCompare(eventName(b)));
    }
    function option(value, label, selected) {
        return '<option value="' + esc(value) + '"' + (value === selected ? ' selected' : '') + '>' + esc(label) + '</option>';
    }
    function filterSelect(id, label, values, selected, allLabel) {
        return '<div class="op-filter' + (['starting', 'ending'].includes(id) ? ' op-week-filter op-' + id + '-filter' : '') + '"><label for="op-' + id + '">' + text(label) + '</label>'
            + '<select id="op-' + id + '" data-filter="' + id + '">'
            + (allLabel ? option('', allLabel, selected) : '')
            + values.map(value => option(value.value || value, value.label || value, selected)).join('')
            + '</select></div>';
    }
    function initWeekSelectors() {
        const $ = window.jQuery;
        if (!$ || !$.fn.select2) return;
        for (const key of ['starting', 'ending']) {
            const select = $('#op-' + key);
            const choices = key === 'ending' ? endingOptions() : startingOptions();
            select.select2({
                width: '100%', minimumResultsForSearch: Infinity,
                dropdownParent: select.parent(), dropdownCssClass: 'op-week-menu op-' + key + '-menu',
                templateResult: item => {
                    const choice = choices.find(choice => choice.value === item.id);
                    if (!choice || !choice.deadlineLabel) return item.text;
                    return $('<span class="op-ending-slam-option"><span class="op-ending-slam-title">' + noTranslate(choice.deadlineLabel)
                        + ' ' + text('Last Week') + '</span><span>' + esc(day(choice.value)) + '</span></span>');
                }
            }).on('change', function () {
                const value = this.value;
                const restoreFocus = restoreControlFocus;
                // Let the picker finish closing before replacing its controls.
                setTimeout(() => {
                    handleChange({target: {dataset: {filter: key}, value}});
                    if (restoreFocus) $('#op-' + key).next('.select2').find('.select2-selection').trigger('focus');
                }, 0);
            });
            select.next('.select2').find('.select2-selection').attr('aria-label', key === 'starting' ? 'Starting week' : 'Ending week');
        }
    }
    function button(action, id, label, active, disabled) {
        const unavailable = action === 'entries' && !(window.WTARG_DATA.tournaments || {})[byId.get(id)?.entryKey || '']?.length;
        const labelMarkup = action === 'clear-schedule'
            ? '<span class="op-clear-full">' + esc(label) + '</span><span class="op-clear-short">Clear</span>' : esc(label);
        return '<button type="button" class="btn ' + (active ? 'btn-primary' : 'btn-secondary')
            + (unavailable ? ' op-entry-unavailable' : '')
            + '" data-action="' + action + '" data-event="' + esc(id) + '"'
            + (disabled ? ' disabled' : '') + (action === 'entries' ? ' aria-expanded="' : ' aria-pressed="') + Boolean(active) + '"'
            + (action === 'entries' ? ' aria-label="' + (unavailable ? 'Entry List unavailable' : 'Entry List') + '"' : '')
            + (unavailable ? ' title="No entry list available yet."' : '')
            + (action === 'schedule' && active ? ' aria-label="Remove from schedule"' : '') + '>' + labelMarkup + '</button>';
    }
    function entryLabel(event) {
        const entry = entryFor(event, state.player);
        if (!entry) return text('Entry status unconfirmed');
        const label = { MAIN: 'Main draw', QUAL: 'Qualifying', ALT: 'Alternate' }[entry.type] || entry.type;
        return text(label);
    }
    function finishSelector(event, prefix) {
        const finishes = finishesFor(event);
        return '<label class="op-finish" for="' + prefix + '-' + esc(event.id) + '">'
            + '<select id="' + prefix + '-' + esc(event.id) + '" data-finish="' + esc(event.id) + '" aria-label="Assumed finish"' + (finishes.length ? '' : ' disabled') + '>'
            + (finishes.length ? finishes.map(finish => option(finish.value, finish.value + ' · ' + finish.points + ' pts', finishFor(event))).join('')
                : option('', 'Point table unavailable', ''))
            + '</select></label>';
    }
    function pointWarnings(result) {
        return result && result.warnings || [];
    }
    function routeSelector(event) {
        const routes = entryRoutes(event, state.player);
        if (!routes.includes('QUAL')) return '';
        const qualifying = routeFor(event) === 'QUAL';
        return button('qualy', event.id, 'Qualy: ' + (qualifying ? '✓' : '□'), qualifying, routes.length < 2);
    }
    function entryListMarkup(event) {
        return '<div class="op-entry-preview" id="op-entry-' + esc(event.id) + '">'
            + (typeof entryListTableMarkup === 'function' ? entryListTableMarkup(event.entryKey || '')
                : '<p>' + text('No entry list available yet.') + '</p>') + '</div>';
    }
    function tournamentLabel(event) {
        if (event.rest) return text('Rest');
        const flag = typeof countryFlag === 'function' ? countryFlag(event.country, false) : noTranslate(event.country);
        return '<span translate="no">' + flag + esc(eventName(event)) + '</span>'
            + (delayedPoints(event) ? '<span class="op-delay-badge" title="These W15/W35 points are added after the Grand Slam cutoff." aria-label="Points added after the Grand Slam cutoff">!</span>' : '');
    }
    function calendarEvent(event) {
        const routes = entryRoutes(event, state.player);
        const available = routes.length > 0;
        const active = available && state.active === event.id;
        const entry = entryFor(event, state.player);
        const uncertain = entry ? entry.type === 'ALT' : event.entryPublished;
        const scheduled = state.schedule.includes(event.id);
        const selected = state.compare.includes(event.id) || scheduled;
        const locked = lockedWeeks(state.player).get(weekFor(event)) === event.id;
        const tourClass = event.level.startsWith('WTA') ? ' cal-tournament-bold cal-tournament-wta' : event.level === 'GS' ? ' cal-tournament-bold' : '';
        return '<div class="op-calendar-item' + (scheduled ? ' op-choice' : '') + '"><button type="button" id="op-pick-' + esc(event.id)
            + '" class="calendar-tournament op-tournament cal-' + slug(event.surface) + tourClass + (selected ? ' op-event-selected' : '')
            + (active ? ' op-event-active' : '')
            + '" data-action="select" data-event="' + esc(event.id)
            + '"' + (locked ? ' disabled title="Tournament fixed for this week"' : available ? '' : ' disabled title="Entries closed"')
            + ' aria-pressed="' + state.schedule.includes(event.id) + '"'
            + (available && uncertain && !locked ? ' title="Conditional on acceptance or a wildcard."' : '') + '>'
            + tournamentLabel(event)
            + '</button>' + (scheduled && !event.rest ? '<div class="op-inline-controls">' + routeSelector(event, 'op-choice-route')
                + finishSelector(event, 'op-choice-finish') + button('entries', event.id, 'Entry List', active) + '</div>' : '')
            + '</div>';
    }
    function calendarMarkup(shown, plan) {
        const weeks = [...new Set(shown.map(weekFor))].sort();
        const regionOrder = ['south_america', 'north_central_america', 'europe', 'africa', 'asia', 'oceania'];
        return '<div class="op-week-list">' + weeks.map(week => {
            const tournaments = shown.filter(event => weekFor(event) === week);
            const chosen = tournaments.find(event => state.schedule.includes(event.id));
            const replacements = chosen && plan ? (plan.replacementsByEvent || {})[chosen.id] || [] : [];
            const replaced = replacements.length ? '<span class="op-week-replaced">' + text('Replaced:') + ' '
                + replacedMarkup({replaced: replacements}) + '</span>' : '';
            const cutoffs = (data.w50CutoffDraws || {})[week]
                || ((data.w50CutoffNames || {})[week] || []).map(name => ({name, draw: ''}));
            const cutoffNames = cutoffs.map(cutoff => slamShortName(cutoff.name) + (cutoff.draw ? ' ' + cutoff.draw : ''));
            const warning = !chosen && tournaments.some(delayedPoints) && cutoffNames.length
                ? '<span class="op-week-note"><strong>!</strong> ' + text('points not added before ' + cutoffNames.join(' / ') + ' deadline') + '</span>' : '';
            const regions = [...new Set(tournaments.map(event => event.continent || ''))].sort((a, b) => {
                const rank = value => regionOrder.includes(value) ? regionOrder.indexOf(value) : regionOrder.length;
                return rank(a) - rank(b) || a.localeCompare(b);
            });
            return '<section class="op-week op-panel" aria-labelledby="op-week-' + esc(week) + '"><h3 class="op-week-heading" id="op-week-' + esc(week) + '">'
                + '<span class="op-week-date">' + esc(shortDate(week)) + '</span>'
                + (warning || replaced ? '<span class="op-week-meta">' + warning + replaced + '</span>' : '') + '</h3>'
                + (chosen ? '<div class="op-calendar-row op-calendar-selected"><div class="op-calendar-events">' + calendarEvent(chosen) + '</div></div>'
                    : regions.map(region => '<div class="op-calendar-row' + (region === 'rest' ? ' op-calendar-rest' : '') + '">'
                        + (region === 'rest' ? '' : '<h4 class="op-calendar-region">' + regionLabel(region) + '</h4>')
                        + '<div class="op-calendar-events">' + tournaments.filter(event => (event.continent || '') === region).map(calendarEvent).join('') + '</div></div>').join(''))
                + (chosen && state.active === chosen.id ? entryListMarkup(chosen) : '') + '</section>';
        }).join('') + '</div>';
    }
    function comparisonMarkup() {
        const selected = state.compare.map(id => byId.get(id)).filter(Boolean);
        if (!selected.length) return '';
        const horizon = endingDate() || selected.map(pointDate).sort().slice(-1)[0];
        const warnings = [...new Set(selected.flatMap(event => pointWarnings(scenario(event, null, horizon))))];
        const rows = [
            ['Date', event => noTranslate(day(event.startDate))],
            ['Access', entryLabel],
            ['Surface', event => text(event.surface)],
            ['Qualy', event => routeSelector(event, 'op-compare-route') || text(routeFor(event) === 'QUAL' ? 'Qualifying' : 'Main draw')],
            ['Finish', event => finishSelector(event, 'op-compare-finish')],
            ['First useful finish', event => text(minimumFinish(event, horizon))],
            ['Gross points', event => numeric((scenario(event, null, horizon) || {}).gross)],
            ['Countable gain', event => '<strong class="op-gain">' + plus((scenario(event, null, horizon) || {}).gain) + '</strong>'],
            ['Replaced tournament:', event => replacedMarkup(scenario(event, null, horizon))]
        ];
        return '<details open class="op-panel op-comparison" aria-labelledby="op-comparison-title"><summary id="op-comparison-title">'
            + text('Compare tournaments') + '</summary><div class="op-section-head"><small>' + noTranslate(day(horizon)) + '</small>'
            + button('clear-compare', '', 'Clear comparison') + '</div><div class="op-table-scroll"><table class="op-compare-table"><thead><tr><th scope="col">'
            + text('Tournament') + '</th>' + selected.map(event => '<th scope="col"><span translate="no">' + esc(eventName(event)) + '</span>'
                + '<button type="button" class="op-remove" data-action="compare" data-event="' + esc(event.id) + '" aria-label="Remove from comparison">×</button></th>').join('')
            + '</tr></thead><tbody>' + rows.map(row => '<tr><th scope="row">' + text(row[0]) + '</th>'
                + selected.map(event => '<td>' + row[1](event) + '</td>').join('') + '</tr>').join('')
            + '</tbody></table></div><p class="op-small-note">' + text('Point scenarios assume the chosen route is available. They do not predict acceptance or match results.') + '</p>'
            + warnings.map(warning => '<p class="op-small-note">' + text(warning) + '</p>').join('') + '</details>';
    }
    function replacedMarkup(result) {
        const replaced = result && result.replaced || [];
        return replaced.length ? replaced.map(item => {
            const name = item.tournament || item.name || '—';
            const label = typeof _formatTournName === 'function' ? _formatTournName(name, item.category, item.matchType) : name;
            return noTranslate(label.replace(/\s+\d+$/, '')) + ' (' + numeric(item.points) + ' pts)';
        }).join('<br>') : text('None');
    }
    function scheduleConflicts(selected) {
        selected = selected.filter(event => !event.rest);
        const conflicts = [];
        for (let i = 0; i < selected.length; i++) {
            for (let j = i + 1; j < selected.length; j++) {
                const a = selected[i], b = selected[j];
                if (a.startDate <= (b.endDate || b.startDate) && b.startDate <= (a.endDate || a.startDate)) {
                    conflicts.push([eventName(a), eventName(b)]);
                }
            }
        }
        return conflicts;
    }
    window.plannerScheduleConflicts = scheduleConflicts;
    function summaryMarkup(result, conflicts) {
        const warnings = result && result.warnings || [];
        return '<section class="op-panel op-summary" aria-label="Projected points">'
            + (conflicts.length ? '<div class="op-warning" role="status">' + text('These tournaments overlap. Remove one before calculating the schedule.')
                + '<ul>' + conflicts.map(pair => '<li>' + noTranslate(pair.join(' / ')) + '</li>').join('') + '</ul></div>'
                : '<div class="op-schedule-summary"><div>' + text('Starting points') + '<strong>' + numeric(result && result.baseline) + '</strong></div><span>+</span><div>'
                    + text('Points Expected') + '<strong class="op-gain">' + (result ? plus(result.gain) : '—') + '</strong></div><span>=</span><div>'
                    + text('Final Points') + '<strong>' + numeric(result && result.total) + '</strong></div></div>')
            + warnings.map(warning => '<p class="op-card-note">' + text(warning) + '</p>').join('')
            + (state.schedule.length ? '<div class="op-summary-actions">' + button('clear-schedule', '', 'Clear selections') + '</div>' : '') + '</section>';
    }
    function breakdownMarkup(plan) {
        if (!plan || !plan.breakdown || typeof _rtgsRenderBreakdown !== 'function') return '';
        const body = {innerHTML: ''};
        _rtgsRenderBreakdown(body, plan.breakdown, true, result => {
            const label = '<span translate="no">' + _rtgsTournamentLabel(result) + '</span>';
            return label + (result._planner ? ' <span class="op-result-new">' + text('New') + '</span>' : '')
                + (result.replacedBy && !result._planner ? ' <span class="op-result-replaced">' + text('Replaced') + '</span>' : '');
        });
        return '<section class="op-panel op-breakdown" aria-labelledby="op-breakdown-title" data-expanded="' + breakdownExpanded + '">'
            + '<h2 id="op-breakdown-title"><span class="op-breakdown-heading">' + text('Points Breakdown') + '</span>'
            + '<button type="button" id="op-breakdown-toggle" data-action="toggle-breakdown"'
            + ' aria-expanded="' + breakdownExpanded + '" aria-controls="op-breakdown-content">' + text('Points Breakdown')
            + '<span class="calendar-toggle-arrow" aria-hidden="true"></span></button></h2>'
            + '<div class="table-wrapper" id="op-breakdown-content"><table id="op-breakdown-table">'
            + '<colgroup><col class="rtgs-col-date"><col class="rtgs-col-tournament"><col class="rtgs-col-round">'
            + '<col class="rtgs-col-points"><col class="rtgs-col-drop-date"></colgroup>'
            + '<thead><tr>' + ['Date', 'Tournament', 'Round', 'PTS', 'Drop Date'].map(label => '<th scope="col">' + text(label) + '</th>').join('')
            + '</tr></thead><tbody id="op-breakdown-body">' + body.innerHTML + '</tbody></table></div></section>';
    }
    function render() {
        const root = document.getElementById('planner-root');
        if (!root) return;
        const player = selectedPlayer();
        if (!players.length) { root.innerHTML = '<p>' + text('No player data is available in this build.') + '</p>'; return; }
        const locked = lockedWeeks(state.player);
        state.schedule = state.schedule.filter(id => !locked.has(weekFor(byId.get(id)))
            || locked.get(weekFor(byId.get(id))) === id);
        for (const id of locked.values()) {
            if (rangeIncludes(byId.get(id)) && !state.schedule.includes(id)) state.schedule.push(id);
        }
        const shown = player ? filteredEvents() : [];
        const planned = state.schedule.map(id => byId.get(id)).filter(Boolean).sort((a, b) => a.startDate.localeCompare(b.startDate));
        const conflicts = scheduleConflicts(planned);
        const horizon = endingDate() || state.target || planned.map(pointDate).sort().pop() || data.asOf;
        const plan = !player || conflicts.length ? null : jointScenario(planned, horizon);
        const weeks = startingOptions();
        const endSelect = filterSelect('ending', 'Ending week', endingOptions(), state.ending);
        if (window.jQuery) window.jQuery('#op-starting, #op-ending').each(function () {
            if (window.jQuery(this).data('select2')) window.jQuery(this).select2('destroy');
        });
        root.innerHTML = '<header class="op-hero"><h1>' + text('Tour Planner') + '</h1></header><div class="op-workspace"><div class="op-toolbar">'
            + '<section class="op-panel op-controls" aria-label="Planner filters">'
            + '<div class="op-primary-filters">' + filterSelect('player', 'Player', players.map(item => ({value:item.id, label:item.name})), state.player, 'Select Player')
            + filterSelect('starting', 'Starting week', weeks, state.starting)
            + endSelect + '</div></section>'
            + '<div class="op-calendar-clear">' + button('clear-schedule', '', 'Clear selections', false, !state.schedule.length) + '</div>'
            + '<div class="op-planner">' + summaryMarkup(plan, conflicts)
            + comparisonMarkup() + '</div></div>'
            + (notice ? '<p class="op-notice" role="status">' + text(notice) + '</p>' : '')
            + '<div class="op-body"><section class="op-results" aria-label="Tournaments">'
            + (!player ? '' : shown.length ? calendarMarkup(shown, plan) : '<p class="op-empty">' + text('No matching tournaments') + '</p>')
            + '</section>' + breakdownMarkup(plan) + '</div></div>';
        initWeekSelectors();
    }
    function allowedIds() {
        if (!selectedPlayer()) return new Set();
        return new Set(eligibleEvents(calendarEvents(), state.player, data.asOf).filter(rangeIncludes).map(event => event.id));
    }
    function changed() {
        const root = document.getElementById('planner-root');
        const activeId = root && root.contains(document.activeElement) && document.activeElement.id;
        render();
        const replacement = activeId && document.getElementById(activeId);
        if (replacement && restoreControlFocus) replacement.focus({ preventScroll: true });
        if (typeof window.syncUrlStateForTab === 'function') window.syncUrlStateForTab('planner');
    }
    function toggleSchedule(id) {
        const event = byId.get(id);
        if (lockedWeeks(state.player).get(weekFor(event)) === id) return;
        if (state.schedule.includes(id)) state.schedule = state.schedule.filter(value => value !== id);
        else {
            if (!event.rest && !finishesFor(event).length) { notice = 'Point table unavailable'; return; }
            state.schedule = state.schedule.filter(value => weekFor(byId.get(value)) !== weekFor(event));
            state.schedule.push(id);
        }
        if (!state.schedule.includes(state.active)) state.active = '';
    }
    function handleClick(event) {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const action = button.dataset.action, id = button.dataset.event;
        notice = '';
        if (action === 'select' || action === 'schedule') {
            if (!allowedIds().has(id)) return;
            toggleSchedule(id);
        } else if (action === 'qualy') {
            const selected = byId.get(id);
            if (!state.schedule.includes(id) || entryRoutes(selected, state.player).length < 2) return;
            const finish = finishFor(selected);
            state.routes[id] = routeFor(selected) === 'QUAL' ? 'MAIN' : 'QUAL';
            if (finishesFor(selected).some(item => item.value === finish)) state.finishes[id] = finish;
            else delete state.finishes[id];
        } else if (action === 'entries') {
            if (!state.schedule.includes(id)) return;
            state.active = state.active === id ? '' : id;
        } else if (action === 'compare') {
            if (!allowedIds().has(id) || byId.get(id).rest) return;
            const list = state[action];
            const index = list.indexOf(id);
            if (index >= 0) list.splice(index, 1);
            else if (action === 'compare' && list.length >= 3) notice = 'Compare up to three tournaments. Remove one to add another.';
            else list.push(id);
        } else if (action === 'toggle-breakdown') breakdownExpanded = !breakdownExpanded;
        else if (action === 'clear-compare') state.compare = [];
        else if (action === 'clear-schedule') { state.schedule = []; state.target = ''; state.active = ''; }
        changed();
    }
    function handleChange(event) {
        const target = event.target;
        notice = '';
        if (target.dataset.filter) {
            const key = target.dataset.filter;
            if (!['player', 'starting', 'ending'].includes(key)) return;
            state[key] = target.value;
            if (key === 'player') {
                state.compare = []; state.schedule = []; state.finishes = {}; state.routes = {}; state.target = ''; state.active = '';
            }
            if (key === 'starting' && endingWeek() < state.starting) state.ending = state.starting;
            if (key === 'starting' || key === 'ending') {
                const inside = id => byId.has(id) && rangeIncludes(byId.get(id));
                state.compare = state.compare.filter(inside); state.schedule = state.schedule.filter(inside);
                if (!inside(state.active)) state.active = '';
            }
        } else if (target.dataset.finish) state.finishes[target.dataset.finish] = target.value;
        else if (target.dataset.route) { state.routes[target.dataset.route] = target.value; delete state.finishes[target.dataset.route]; }
        else if (target.id === 'op-target') state.target = target.value;
        else return;
        changed();
    }
    window.initPlanner = async function () {
        const root = document.getElementById('planner-root');
        if (!root) return;
        if (typeof ensureHistoryDataLoaded === 'function') {
            if (!mounted) root.innerHTML = '<div class="op-panel op-empty" role="status">' + text('Loading player results…') + '</div>';
            try { await ensureHistoryDataLoaded(); }
            catch (_) {
                root.innerHTML = '<div class="op-panel op-empty" role="alert">' + text('Player results could not be loaded. Reload the page to retry.') + '</div>';
                return false;
            }
        }
        if (!mounted) {
            root.addEventListener('pointerdown', event => { restoreControlFocus = event.pointerType === 'mouse'; }, true);
            root.addEventListener('keydown', () => { restoreControlFocus = true; }, true);
            root.addEventListener('click', handleClick);
            root.addEventListener('change', handleChange);
            mounted = true;
        }
        const weeks = weekOptions();
        if (!state.starting) state.starting = weeks[0] || '';
        if (!state.ending) state.ending = weeks[Math.min(weeks.indexOf(state.starting) + 3, weeks.length - 1)] || '';
        if (!endingOptions().some(item => item.value === state.ending)) state.ending = (endingOptions().slice(-1)[0] || {}).value || '';
        render();
        return true;
    };
    window.collectPlannerUrlState = function () {
        const result = { player: slug((selectedPlayer() || {}).name) };
        for (const key of ['starting', 'ending']) if (state[key]) result[key] = state[key];
        if (state.compare.length) result.compare = state.compare;
        if (state.schedule.length) result.schedule = state.schedule;
        if (state.target) result.target = state.target;
        const selected = [...new Set(state.compare.concat(state.schedule))].filter(id => !byId.get(id).rest);
        if (selected.length) result.results = selected.map(id => id + '~' + finishFor(byId.get(id)) + '~' + routeFor(byId.get(id)));
        return result;
    };
    window.restorePlannerUrlState = async function (params) {
        if (!await window.initPlanner()) return;
        const player = players.find(item => item.id === params.get('player') || slug(item.name) === slug(params.get('player')));
        if (player) state.player = player.id;
        const weeks = weekOptions();
        if (weeks.includes(params.get('starting') || params.get('week'))) state.starting = params.get('starting') || params.get('week');
        const savedSlam = slamOptions().find(item => item.value === params.get('ending'));
        const ending = savedSlam ? savedSlam.week : params.get('ending');
        if (endingOptions().some(item => item.value === ending)) state.ending = ending;
        if (endingWeek() < state.starting) state.ending = state.starting;
        const allowed = allowedIds();
        const list = key => [...new Set((params.get(key) || '').split(',').filter(id => allowed.has(id)))];
        state.compare = list('compare').filter(id => !byId.get(id).rest).slice(0, 3);
        const scheduledWeeks = new Set();
        state.schedule = list('schedule').filter(id => {
            const week = weekFor(byId.get(id));
            if (scheduledWeeks.has(week)) return false;
            scheduledWeeks.add(week);
            return true;
        });
        state.active = '';
        state.finishes = {}; state.routes = {};
        (params.get('results') || '').split(',').forEach(value => {
            const parts = value.split('~');
            if (!allowed.has(parts[0])) return;
            state.routes[parts[0]] = parts[2];
            if (finishesFor(byId.get(parts[0])).some(finish => finish.value === parts[1])) state.finishes[parts[0]] = parts[1];
        });
        state.target = /^\d{4}-\d{2}-\d{2}$/.test(params.get('target') || '') ? params.get('target') : '';
        render();
    };
})();
