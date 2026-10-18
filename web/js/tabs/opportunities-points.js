/* Point scenarios share the same counted-result rules as Points Breakdown. */
(function (global) {
    'use strict';
    const cache = new Map();
    const labels = {QR1: 'Qualifying round 1', QR2: 'Qualifying round 2', QR3: 'Qualifying round 3',
        R128: 'Round of 128', R64: 'Round of 64', R32: 'Round of 32', R16: 'Round of 16',
        QF: 'Quarterfinal', SF: 'Semifinal', F: 'Final', W: 'Title'};
    const rounds = ['QR1', 'QR2', 'QR3', 'R128', 'R64', 'R32', 'R16', 'QF', 'SF', 'F', 'W'];
    const day = (value) => String(value || '').slice(0, 10);
    const date = (value) => new Date(day(value) + 'T00:00:00Z');
    const addDays = (value, days) => {
        const result = date(value);
        result.setUTCDate(result.getUTCDate() + days);
        return result.toISOString().slice(0, 10);
    };
    function pointTable(event) {
        const data = global.WTARG_DATA || {};
        const rows = data.pointsDistribution || [];
        const sizes = [...(data.itfDrawSizes || []), ...(data.wtaDrawSizes || [])];
        const known = sizes.find(row => row.date === event.startDate &&
            ((row.tournamentId && String(row.tournamentId) === String(event.tournamentId || event.sourceId)) ||
            row.tournamentName === event.name));
        const level = event.level;
        const description = known?.description || event.pointsDescription ||
            (typeof _rtgs_categoryToDesc !== 'undefined' ? _rtgs_categoryToDesc[level] : null);
        const sizeMatch = String(description || '').match(/(\d+)M/);
        const standardSize = typeof _rtgs_categoryDrawSize !== 'undefined' ? _rtgs_categoryDrawSize[level] : null;
        const size = known?.mainDrawSize || event.drawSize || Number(sizeMatch?.[1]) || standardSize || 32;
        const firstRound = size > 64 ? 'R128' : size > 32 ? 'R64' : 'R32';
        return {row: rows.find(row => row.Description === description), firstRound};
    }
    function routeFor(event, route) {
        return route || event.assumedRoute || 'MAIN';
    }
    function finishes(event, route) {
        const {row, firstRound} = pointTable(event);
        if (!row) return [];
        const qualifying = routeFor(event, route) === 'QUAL';
        const bonus = qualifying ? Number(row.QLFR) || 0 : 0;
        return rounds.filter(key => (qualifying || !key.startsWith('QR')) &&
            (key === firstRound || (qualifying && key === 'QR1') ||
                (row[key] !== null && row[key] !== undefined && row[key] !== '')))
            .map(key => ({value: key, label: labels[key],
                points: (qualifying && key === firstRound ? 0 : Number(row[key]) || 0) +
                    (key.startsWith('QR') ? 0 : bonus)}));
    }
    function pointDate(event) {
        if (!event.endDate) return '';
        const end = date(event.endDate);
        const daysToMonday = end.getUTCDay() === 0 ? 1 : 8 - end.getUTCDay();
        return addDays(event.endDate, daysToMonday + (['W15', 'W35'].includes(event.level) ? 7 : 0));
    }
    function breakdown(playerName, horizon) {
        const key = playerName + '|' + horizon;
        if (!cache.has(key)) cache.set(key, _rtgsComputeBreakdown(playerName.toUpperCase(), horizon));
        return cache.get(key);
    }
    function sameEvent(result, event) {
        const name = (value) => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toUpperCase().replace(/[^A-Z0-9]/g, '');
        const distance = Math.abs(date(result.date) - date(event.startDate)) / 86400000;
        if (event.tournamentId && result.tournamentId) {
            return distance <= 21 && String(result.tournamentId) === String(event.tournamentId);
        }
        return distance <= 7 && name(result.tournament) === name(event.name);
    }
    function scheduleScenario(playerName, selections, horizon) {
        const asOf = global.WTARG_DATA.opportunities?.asOf || new Date().toISOString().slice(0, 10);
        const lastPublication = selections.map(item => pointDate(item.event)).filter(Boolean).sort().pop() || asOf;
        horizon = day(horizon || lastPublication);
        const aoCutoff = (global.WTARG_DATA.opportunities?.gsDeadlines || []).some(gs =>
            gs.name === 'Australian Open' && (gs.mdCutoff === horizon || gs.qCutoff === horizon));
        // The site's Sunday AO cutoff includes that week's Monday ranking publication.
        const publicationCutoff = aoCutoff && date(horizon).getUTCDay() === 0 ? addDays(horizon, 1) : horizon;
        const warnings = [];
        const original = breakdown(playerName, horizon);
        const results = [...original.countable, ...original.nonCountable];
        const currentTotal = breakdown(playerName, asOf).totalPoints;
        const player = (global.WTARG_DATA.opportunities?.players || []).find(p => p.name === playerName);
        const officialPoints = Number.isFinite(player?.points) ? player.points : null;
        const reconciliationGap = officialPoints === null ? null : currentTotal - officialPoints;
        let gross = 0;
        const hypothetical = [];
        const acceptedEvents = [];
        const unique = new Set();
        for (const item of selections) {
            const {event, finish, route} = item;
            if (unique.has(event.id)) continue;
            unique.add(event.id);
            const option = finishes(event, route).find(option => option.value === finish);
            if (!option) { warnings.push('Point table unavailable for a selected result.'); continue; }
            const effectiveDate = pointDate(event);
            if (!effectiveDate || effectiveDate > publicationCutoff) {
                warnings.push('A selected result is not published by the target date.');
                continue;
            }
            if (addDays(effectiveDate, 364) <= horizon) {
                warnings.push('A selected result has expired by the target date.');
                continue;
            }
            gross += option.points;
            acceptedEvents.push(event);
            hypothetical.push({date: event.startDate, tournament: event.name, _opportunityId: event.id,
                tournamentId: event.tournamentId || event.sourceId || event.id, category: event.level,
                isGS: event.level === 'GS', bestMainRound: finish.startsWith('QR') ? '' : finish,
                points: option.points, _opportunity: true});
        }
        const retained = results.filter(result => !acceptedEvents.some(event => sameEvent(result, event)));
        hypothetical.sort((a, b) => a.date.localeCompare(b.date));
        const total = _rtgsSelectCountable([...retained, ...hypothetical]);
        const replaced = original.countable.filter(result => !total.countable.includes(result));
        const replacementsByEvent = {};
        const prefix = [];
        let previous = original;
        for (const planned of hypothetical) {
            prefix.push(planned);
            const chosen = acceptedEvents.filter(event => prefix.some(item => item._opportunityId === event.id));
            const ledger = results.filter(result => !chosen.some(event => sameEvent(result, event)));
            const step = _rtgsSelectCountable([...ledger, ...prefix]);
            const removed = previous.countable.filter(result => !step.countable.includes(result));
            replacementsByEvent[planned._opportunityId] = removed.flatMap(result =>
                result._opportunity ? replacementsByEvent[result._opportunityId] || [] : [result]);
            for (const result of removed) {
                if (result._opportunity) replacementsByEvent[result._opportunityId] = [];
            }
            previous = step;
        }
        const assigned = new Set();
        for (const id of Object.keys(replacementsByEvent)) {
            replacementsByEvent[id] = replacementsByEvent[id].filter(result => {
                if (!replaced.includes(result) || assigned.has(result)) return false;
                assigned.add(result);
                return true;
            });
        }
        return {gross, gain: total.totalPoints - original.totalPoints, total: total.totalPoints,
            baseline: original.totalPoints, currentTotal, officialPoints, reconciliationGap,
            replaced, replacementsByEvent, horizon, effectiveDate: lastPublication, warnings: [...new Set(warnings)]};
    }
    global.opportunitiesPointFinishes = finishes;
    global.opportunitiesPointDate = pointDate;
    global.opportunitiesScheduleScenario = scheduleScenario;
    global.opportunitiesPointScenario = function (playerName, event, finish, horizon, otherEvents = []) {
        return scheduleScenario(playerName, [...otherEvents, {event, finish, route: event.assumedRoute}], horizon);
    };
})(window);
