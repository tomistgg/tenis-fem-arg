import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js unavailable")


def run_points_check(check):
    """Use the real shared selection rules with a controlled ranking ledger."""
    script = r"""
        const fs = require('fs');
        const vm = require('vm');
        const assert = require('assert/strict');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const table = {Description: 'W50 (32M, 32Q)', W: 50, F: 33, SF: 20,
            QF: 11, R16: 6, R32: 1, QLFR: 2, QR2: 1};
        const context = {document: {addEventListener() {}}, console,
            WTARG_DATA: {pointsDistribution: [table], itfDrawSizes: [], wtaDrawSizes: [],
                opportunities: {asOf: '2026-10-12', players: [{name: 'Test Player', points: 1710}]}}};
        context.window = context;
        vm.createContext(context);
        vm.runInContext(input.ledger, context);
        const select = context._rtgsSelectCountable;
        const realBreakdown = context._rtgsComputeBreakdown;
        const result = (points, index) => ({points, tournament: 'Past ' + index,
            tournamentId: 'past-' + index, category: 'W50', bestMainRound: 'SF',
            date: '2026-09-07'});
        let baseline = [...Array.from({length: 17}, (_, index) => result(100, index)), result(10, 18)];
        context._rtgsComputeBreakdown = () => select(baseline);
        vm.runInContext(input.points, context);
        const event = {id: 'next', tournamentId: 'next-id', name: 'Next Tournament', level: 'W50',
            startDate: '2026-10-19', endDate: '2026-10-25', assumedRoute: 'MAIN',
            pointsDescription: table.Description};
        const scenario = (...args) => context.opportunitiesPointScenario('Test Player', ...args);
        const schedule = (...args) => context.opportunitiesScheduleScenario('Test Player', ...args);
        const finish = (round, route) => context.opportunitiesPointFinishes(event, route)
            .find(item => item.value === round);
    """ + check
    payload = {
        "ledger": (PROJECT_ROOT / "web/js/tabs/roadtogs.js").read_text(encoding="utf-8"),
        "points": (PROJECT_ROOT / "web/js/tabs/opportunities-points.js").read_text(encoding="utf-8"),
    }
    completed = subprocess.run(
        [NODE, "-e", script], input=json.dumps(payload), text=True, capture_output=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr


def test_points_gain_replaces_the_weakest_counted_result():
    run_points_check("""
        const value = scenario(event, 'SF');
        assert.equal(value.baseline, 1710);
        assert.equal(value.gross, 20);
        assert.equal(value.total, 1720);
        assert.equal(value.gain, 10);
        assert.equal(value.replaced.length, 1);
        assert.equal(value.replaced[0].points, 10);
        assert.equal(value.reconciliationGap, 0);
    """)


def test_sunday_ao_cutoff_includes_its_ranking_week_without_w15_w35_delay():
    run_points_check("""
        context.WTARG_DATA.opportunities.gsDeadlines = [{name: 'Australian Open',
            mdCutoff: '2026-11-15', qCutoff: '2026-11-15'}];
        const finalWeek = {...event, startDate: '2026-11-09', endDate: '2026-11-15'};
        assert.equal(scenario(finalWeek, 'SF', '2026-11-15').gross, 20);
        const delayed = {...finalWeek, level: 'W35'};
        assert.equal(scenario(delayed, 'SF', '2026-11-15').gross, 0);
        const earlier = {...delayed, startDate: '2026-11-02', endDate: '2026-11-08'};
        assert.equal(scenario(earlier, 'SF', '2026-11-15').gross, 20);
        assert.equal(scenario(finalWeek, 'SF', '2026-11-14').gross, 0);
    """)


def test_qualifying_awards_match_the_existing_points_ledger():
    run_points_check("""
        assert.equal(finish('QR2', 'QUAL').points, 1);
        assert.equal(finish('R16', 'QUAL').points, 8);
        assert.equal(finish('R32', 'QUAL').points, 2);
        assert.equal(finish('R32', 'MAIN').points, 1);
        assert.equal(finish('QR2', 'MAIN'), undefined);
    """)


def test_first_round_awards_use_the_draw_size_and_allow_zero_point_results():
    run_points_check("""
        context.WTARG_DATA.pointsDistribution.push({Description: 'Grand Slam',
            R128: 10, R64: 70, R32: 130, R16: 240, QLFR: 40, QR3: 30});
        context.WTARG_DATA.pointsDistribution.push({Description: 'W35 (32M, 64/48/32/24Q)',
            R32: null, R16: 4, QLFR: 1});
        const grandSlam = {...event, level: 'GS', pointsDescription: 'Grand Slam'};
        const gsFinishes = context.opportunitiesPointFinishes(grandSlam, 'QUAL');
        assert.equal(gsFinishes.find(item => item.value === 'R128').points, 40);
        assert.equal(gsFinishes.find(item => item.value === 'R32').points, 170);
        const lowerTier = {...event, level: 'W35', pointsDescription: 'W35 (32M, 64/48/32/24Q)'};
        const md = context.opportunitiesPointFinishes(lowerTier, 'MAIN');
        const qualifying = context.opportunitiesPointFinishes(lowerTier, 'QUAL');
        assert.equal(md.find(item => item.value === 'R32').points, 0);
        assert.equal(qualifying.find(item => item.value === 'R32').points, 1);
    """)


def test_schedule_recomputes_joint_gain_instead_of_adding_individual_gains():
    run_points_check("""
        const second = {...event, id: 'second', tournamentId: 'second-id', name: 'Second Tournament',
            startDate: '2026-10-26', endDate: '2026-11-01'};
        const horizon = '2026-11-02';
        const firstGain = scenario(event, 'SF', horizon).gain;
        const secondGain = scenario(second, 'SF', horizon).gain;
        const combined = schedule([{event, finish: 'SF'}, {event: second, finish: 'SF'}], horizon);
        assert.equal(firstGain + secondGain, 20);
        assert.equal(combined.gross, 40);
        assert.equal(combined.gain, 10);
        assert.equal(combined.total, 1720);
        assert.equal(combined.replacementsByEvent.next.length, 1);
        assert.equal(combined.replacementsByEvent.second.length, 0);
    """)


def test_later_better_result_inherits_the_replacement_from_an_uncounted_planned_event():
    run_points_check("""
        const second={...event,id:'second',tournamentId:'second-id',startDate:'2026-10-26',endDate:'2026-11-01'};
        const value=schedule([{event,finish:'SF'},{event:second,finish:'W'}], '2026-11-02');
        assert.equal(value.replacementsByEvent.next.length,0);
        assert.equal(value.replacementsByEvent.second.length,1);
        assert.equal(value.replacementsByEvent.second[0].points,10);
        assert.equal(value.gain,40);
    """)


def test_counted_planned_results_get_distinct_replacements_in_date_order():
    run_points_check("""
        baseline=[...Array.from({length:16},(_,index)=>result(100,index)),result(6,17),result(4,18)];
        const second={...event,id:'second',tournamentId:'second-id',startDate:'2026-10-26',endDate:'2026-11-01'};
        const value=schedule([{event:second,finish:'F'},{event,finish:'SF'}], '2026-11-02');
        assert.deepEqual(Array.from(value.replacementsByEvent.next,p=>p.points),[4]);
        assert.deepEqual(Array.from(value.replacementsByEvent.second,p=>p.points),[6]);
        assert.equal(value.gain,43);
    """)


def test_points_publication_delay_and_target_date():
    run_points_check("""
        assert.equal(context.opportunitiesPointDate(event), '2026-10-26');
        assert.equal(context.opportunitiesPointDate({...event, level: 'W15'}), '2026-11-02');
        assert.equal(context.opportunitiesPointDate({...event, level: 'W35'}), '2026-11-02');
        const future = scenario(event, 'SF', '2026-10-25');
        assert.equal(future.gross, 0);
        assert.equal(future.gain, 0);
        assert.equal(future.total, 1710);
        assert(future.warnings.some(warning => warning.includes('not published')));
    """)


def test_a_scenario_replaces_the_existing_result_from_the_same_event():
    run_points_check("""
        baseline = [result(100, 1), {...result(6, 2), tournament: event.name,
            tournamentId: event.tournamentId, date: event.startDate}];
        const value = scenario(event, 'SF');
        assert.equal(value.baseline, 106);
        assert.equal(value.gross, 20);
        assert.equal(value.total, 120);
        assert.equal(value.gain, 14);
        assert.equal(value.replaced.length, 1);
        assert.equal(value.replaced[0].points, 6);
    """)


def test_unpublished_scenarios_do_not_remove_existing_event_results():
    run_points_check("""
        baseline = [{...result(6, 1), tournament: event.name,
            tournamentId: event.tournamentId, date: event.startDate}];
        const value = scenario(event, 'SF', '2026-10-25');
        assert.equal(value.baseline, 6);
        assert.equal(value.gross, 0);
        assert.equal(value.gain, 0);
        assert.equal(value.total, 6);
        assert.equal(value.replaced.length, 0);
    """)


def test_same_named_events_with_different_ids_remain_separate_results():
    run_points_check("""
        baseline = [{...result(6, 1), tournament: event.name,
            tournamentId: 'different-edition-id', date: '2026-10-05'}];
        const value = scenario(event, 'SF');
        assert.equal(value.baseline, 6);
        assert.equal(value.total, 26);
        assert.equal(value.gain, 20);
        assert.equal(value.replaced.length, 0);
    """)


def test_reconstruction_difference_is_retained_without_a_warning_or_ledger_adjustment():
    run_points_check("""
        context.WTARG_DATA.opportunities.players[0].points = 1800;
        const value = scenario(event, 'SF');
        assert.equal(value.currentTotal, 1710);
        assert.equal(value.officialPoints, 1800);
        assert.equal(value.reconciliationGap, -90);
        assert.equal(value.gain, 10);
        assert(!value.warnings.some(warning => warning.includes('official ranking')));
    """)


def test_recorded_w15_w35_points_wait_until_the_second_monday_without_moving_expiry():
    run_points_check("""
        context.pointsDistribution = [
            {Description:'W15 (32M, 64/48/32/24Q)',SF:6},
            {Description:'W35 (32M, 64/48/32/24Q)',SF:14}];
        context.itfDrawSizes=[];context.wtaDrawSizes=[];
        context.getDisplayName=name=>name;context.abbrevRound=round=>round;
        context.historyData=['W15','W35'].map(category=>({
            DATE:'2026-10-11',TOURNAMENT:category+' Cipolletti',TOURNAMENT_ID:category,
            CATEGORY:category,MATCH_TYPE:'ITF',DRAW:'M',ROUND:'Semi-finals',
            _winnerName:'Opponent',_loserName:'Test Player'}));
        assert.equal(realBreakdown('TEST PLAYER','2026-10-12').totalPoints,0);
        assert.equal(realBreakdown('TEST PLAYER','2026-10-18').totalPoints,0);
        const published=realBreakdown('TEST PLAYER','2026-10-19');
        assert.equal(published.totalPoints,20);
        assert(published.countable.every(t=>t.dropDate==='2027-10-18'));
        assert.equal(realBreakdown('TEST PLAYER','2027-10-18').totalPoints,0);
        assert.equal(context.opportunitiesPointDate({level:'W15',endDate:'2026-10-11'}),'2026-10-19');
    """)


def run_eligibility_check(check):
    script = r"""
        const fs = require('fs');
        const vm = require('vm');
        const assert = require('assert/strict');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const common = {name: 'W50 Fixture Event 3', source: 'ITF', level: 'W50', startDate: '2026-11-02',
            endDate: '2026-11-08', deadline: '2026-10-22', openRoutes: ['MAIN', 'QUAL'], entries: [],
            country: 'ARG', surface: 'Clay'};
        const events = [
            {...common, id: 'mine', entryPublished: true,
                entries: [{playerKey: 'player-1', type: 'MAIN'}]},
            {...common, id: 'alternate', entryPublished: true,
                entries: [{playerKey: 'player-1', type: 'ALT', position: 7}]},
            {...common, id: 'absent', entryPublished: true,
                entries: [{playerKey: 'player-2', type: 'MAIN'}]},
            {...common, id: 'open', source: 'WTA', entryPublished: false, openRoutes: ['QUAL']},
            {...common, id: 'closed', entryPublished: false, deadline: '2026-10-17'},
            {...common, id: 'deadline-today', entryPublished: false, deadline: '2026-10-18'},
            {...common, id: 'unknown', entryPublished: false, deadline: null},
            {...common, id: 'no-route', entryPublished: false, openRoutes: []},
            {...common, id: 'past', startDate: '2026-10-12', entryPublished: true,
                entries: [{playerKey: 'player-1', type: 'MAIN'}]},
        ];
        const handlers = {};
        const root = {innerHTML: '', contains: () => false,
            addEventListener(type, callback) { handlers[type] = callback; }};
        const context = {document: {getElementById: () => root, querySelector: () => null}, console,
            WTARG_DATA: {opportunities: {asOf: '2026-10-18', events,
                players: [{id: 'player-1', name: 'Test Player', rank: 300, points: 200}]}},
            ensureHistoryDataLoaded: async () => {},
            opportunitiesPointFinishes: () => [{value: 'QF', label: 'Quarterfinal', points: 11}],
            opportunitiesPointDate: () => '2026-11-09',
            opportunitiesPointScenario: () => ({gross: 11, gain: 5, replaced: []}),
            tournamentData:{}, countryFlag:()=>'', getDisplayName:value=>value};
        context.window = context;
        vm.createContext(context);
        vm.runInContext(input.formatter, context);
        vm.runInContext(input.entries, context);
        vm.runInContext(input.frontend, context);
        const eligible = () => Array.from(context.opportunitiesEligibleEvents(events,
            'player-1', '2026-10-18'), event => event.id);
        const click = (action, id) => handlers.click({target: {closest: () => ({dataset: {action, event: id}})}});
        (async () => {
    """ + check + "\n})().catch(error => {console.error(error); process.exitCode = 1;});"
    app = (PROJECT_ROOT / "web/js/app.js").read_text(encoding="utf-8")
    payload = {
        "frontend": (PROJECT_ROOT / "web/js/tabs/opportunities.js").read_text(encoding="utf-8"),
        "formatter": app[app.index("function _formatTournName("):app.index("function getRoundFilterLabel(")],
        "entries": (
            app[app.index("function entryByPos("):app.index("function entryDrawStrengthGM(")]
            + app[app.index("function renderRows("):app.index("function updateEntryList(")]
            + app[app.index("function escapeHtml("):app.index("function normalizeHistoryFilterValues(")]
        ),
    }
    completed = subprocess.run(
        [NODE, "-e", script], input=json.dumps(payload), text=True, capture_output=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr


def test_planner_starts_without_a_player_and_clears_the_tour_when_deselected():
    run_eligibility_check("""
        await context.initOpportunities();
        assert.equal(context.collectOpportunitiesUrlState().player,'');
        assert(root.innerHTML.includes('value="" selected>Select Player</option>'));
        assert(!root.innerHTML.includes('data-action="select"'));
        click('select','mine');
        assert(!context.collectOpportunitiesUrlState().schedule);
        handlers.change({target:{dataset:{filter:'player'},value:'player-1'}});
        assert.equal(context.collectOpportunitiesUrlState().player,'test-player');
        click('select','mine');
        assert.deepEqual(Array.from(context.collectOpportunitiesUrlState().schedule),['mine']);
        handlers.change({target:{dataset:{filter:'player'},value:''}});
        assert.equal(context.collectOpportunitiesUrlState().player,'');
        assert(!context.collectOpportunitiesUrlState().schedule);
        assert(!root.innerHTML.includes('data-action="select"'));
    """)


def test_all_future_tournaments_allow_wildcards_and_default_to_main_draw():
    run_eligibility_check("""
        assert.deepEqual(eligible(), ['mine','alternate','absent','open','closed',
            'deadline-today','unknown','no-route']);
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        assert(!root.innerHTML.includes('op-entry-uncertain'));
        assert(root.innerHTML.includes('Conditional on acceptance or a wildcard.'));
        for (const id of eligible()) {
            click('select',id);
            assert(context.collectOpportunitiesUrlState().results[0].endsWith('~MAIN'));
            if (id !== 'mine') {
                click('qualy',id);
                assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QUAL'));
                click('qualy',id);
                assert(context.collectOpportunitiesUrlState().results[0].endsWith('~MAIN'));
            }
            click('clear-schedule','');
        }
    """)


def test_existing_wta_entry_does_not_hide_itf_tournaments_with_open_deadlines():
    run_eligibility_check("""
        events.find(event => event.id === 'mine').source = 'WTA';
        events.push({...common, id: 'both-open', source: 'WTA', openRoutes: ['MAIN', 'QUAL']},
            {...common, id: 'itf-open', openRoutes: ['MAIN', 'QUAL']},
            {...common, id: 'main-only', source: 'WTA', openRoutes: ['MAIN']},
            {...common, id: 'unlocked', startDate: '2026-11-09', openRoutes: ['MAIN', 'QUAL']});
        const routes = id => Array.from(context.opportunitiesEntryRoutes(
            events.find(event => event.id === id), 'player-1'));
        assert.deepEqual(routes('mine'), ['MAIN']);
        assert.deepEqual(routes('both-open'), ['MAIN', 'QUAL']);
        assert.deepEqual(routes('itf-open'), ['MAIN', 'QUAL']);
        assert.deepEqual(routes('main-only'), ['MAIN', 'QUAL']);
        assert.deepEqual(routes('unlocked'), ['MAIN', 'QUAL']);
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        assert(root.innerHTML.includes('data-event="both-open"'));
        assert(!root.innerHTML.includes('QUALY ONLY'));
        assert(root.innerHTML.includes('data-event="main-only"'));
        assert(root.innerHTML.includes('data-event="absent"'));
        assert(root.innerHTML.includes('data-event="itf-open"'));
        assert(!root.innerHTML.includes('Fixture Event 3'));
    """)


def test_unlisted_wta_players_can_assume_main_or_qualifying_as_alternates_after_deadlines():
    run_eligibility_check("""
        const event = {...common, source: 'WTA', entryPublished: true, openRoutes: ['QUAL'],
            deadlineMain: '2026-10-12', deadlineQual: '2026-10-19'};
        assert.deepEqual(Array.from(context.opportunitiesEntryRoutes(event, 'player-1')), ['MAIN','QUAL']);
        event.deadlineQual = '2026-10-18';
        event.deadlineMain = '2026-10-01';
        event.openRoutes = [];
        assert.deepEqual(Array.from(context.opportunitiesEntryRoutes(event, 'player-1')), ['MAIN','QUAL']);
        Object.assign(events.find(e=>e.id==='alternate'),event,{id:'alternate',entries:[]});
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        click('select','alternate');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~MAIN'));
        click('qualy','alternate');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QUAL'));
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player',
            schedule:'alternate',results:'alternate~QF~QUAL'}));
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QUAL'));
    """)


def test_delay_markers_appear_only_for_w15_w35_in_w50_cutoff_weeks():
    run_eligibility_check("""
        context.WTARG_DATA.opportunities.w50CutoffWeeks = ['2026-11-09'];
        context.WTARG_DATA.opportunities.w50CutoffDraws = {
            '2026-11-09': [{name:'Australian Open',draw:''}]};
        events.push({...common, id: 'earlier', level: 'W35',
                entries: [{playerKey: 'player-1', type: 'MAIN', position: 8}]},
            {...common, id: 'delayed', level: 'W35', startDate: '2026-11-09'},
            {...common, id: 'on-time', level: 'W50', startDate: '2026-11-09'});
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        const button = id => root.innerHTML.match(new RegExp('<button[^>]*data-event="' + id + '".*?</button>'))[0];
        assert(button('delayed').includes('op-delay-badge'));
        assert(button('delayed').includes('>!</span>'));
        assert(!button('earlier').includes('op-delay-badge'));
        assert(!button('on-time').includes('op-delay-badge'));
        assert(!root.innerHTML.includes('+1w'));
        assert(!root.innerHTML.includes('Main draw #8'));
        assert(!root.innerHTML.includes('op-calendar-entry'));
        assert(root.innerHTML.includes('points not added before AO deadline'));
        assert(root.innerHTML.indexOf('op-week-note') < root.innerHTML.lastIndexOf('</h3>'));
        context.WTARG_DATA.opportunities.w50CutoffDraws['2026-11-09'] = [{name:'Roland Garros',draw:'Q'}];
        click('clear-schedule', '');
        assert(root.innerHTML.includes('points not added before RG Q deadline'));
        const selectable=events.find(event=>event.id==='open');
        selectable.startDate='2026-11-09';selectable.level='W35';
        click('select','open');
        assert(!root.innerHTML.includes('op-week-note'));
        click('select','open');
        assert(root.innerHTML.includes('points not added before RG Q deadline'));
    """)


def test_category_order_places_wta_before_itf_and_higher_itf_levels_first():
    run_eligibility_check("""
        events.push({...common, id: 'w15', level: 'W15', name: 'W15 City 2',
                entries: [{playerKey: 'player-1', type: 'MAIN'}]},
            {...common, id: 'wta125', source: 'WTA', level: 'WTA 125', name: 'WTA 125 City'},
            {...common, id: 'w100', level: 'W100', name: 'W100 City',
                entries: [{playerKey: 'player-1', type: 'MAIN'}]});
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        const position = id => root.innerHTML.indexOf('data-event="' + id + '"');
        assert(position('wta125') < position('w100'));
        assert(position('w100') < position('mine'));
        assert(position('mine') < position('w15'));
    """)


def test_url_rejects_unknown_or_past_events_and_preserves_an_existing_main_draw_entry():
    run_eligibility_check("""
        const query = new URLSearchParams({player: 'test-player', mode: 'entries',
            compare: 'nonexistent,past,mine,mine',
            schedule: 'nonexistent,past,mine,open,mine',
            results: 'nonexistent~QF~MAIN,mine~QF~QUAL,open~FORGED~MAIN'});
        await context.restoreOpportunitiesUrlState(query);
        const state = context.collectOpportunitiesUrlState();
        assert.deepEqual(Array.from(state.compare), ['mine']);
        assert.deepEqual(Array.from(state.schedule), ['mine']);
        assert.deepEqual(Array.from(state.results), ['mine~QF~MAIN']);
    """)


def test_calendar_clicks_toggle_the_schedule_and_restore_other_options_in_that_week():
    run_eligibility_check("""
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        click('select', 'mine');
        assert.deepEqual(Array.from(context.collectOpportunitiesUrlState().schedule), ['mine']);
        assert(!root.innerHTML.includes('data-event="open"'));
        assert(!root.innerHTML.includes('data-event="alternate"'));
        assert(root.innerHTML.includes('aria-pressed="true"'));
        assert(root.innerHTML.indexOf('op-summary') < root.innerHTML.indexOf('op-results'));
        assert(root.innerHTML.includes('op-choice-finish-mine'));
        assert(!root.innerHTML.includes('op-schedule-events'));
        click('select', 'mine');
        assert.equal(context.collectOpportunitiesUrlState().schedule, undefined);
        assert(!root.innerHTML.includes('op-choice-finish-mine'));
        assert(root.innerHTML.includes('data-event="open"'));
        click('select', 'open');
        assert.deepEqual(Array.from(context.collectOpportunitiesUrlState().schedule), ['open']);
        click('schedule', 'open');
        assert.equal(context.collectOpportunitiesUrlState().schedule, undefined);
        assert(root.innerHTML.includes('data-event="mine"'));
    """)


def test_displaced_results_use_shared_compact_tournament_names_and_point_units():
    run_eligibility_check("""
        const replaced = [
            {tournament:'Cincinnati Open - Cincinnati, OH', category:'WTA 1000', matchType:'WTA', points:2},
            {tournament:'Open - Buenos Aires, ARG', category:'WTA 125', matchType:'WTA', points:5},
            {tournament:'W35 Monastir 3', category:'W35', matchType:'ITF', points:8},
            {tournament:'Wimbledon', category:'GS', matchType:'GS', points:10},
        ];
        context.opportunitiesScheduleScenario = () => ({baseline:200, gain:5, total:205,
            replaced, replacementsByEvent:{mine:replaced}});
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        click('select', 'mine');
        const rendered = root.innerHTML.replace(/<[^>]*>/g, '');
        assert(rendered.includes('WTA 1000 Cincinnati (2 pts)'));
        assert(rendered.includes('WTA 125 Buenos Aires (5 pts)'));
        assert(rendered.includes('W35 Monastir (8 pts)'));
        assert(rendered.includes('Wimbledon (10 pts)'));
        assert(!rendered.includes('Cincinnati Open'));
        assert(rendered.includes('Replaced:'));
        assert(root.innerHTML.includes('<span class="op-week-replaced">'));
        assert(root.innerHTML.indexOf('op-week-replaced') < root.innerHTML.indexOf('</h3>'));
    """)


def test_qualy_toggle_updates_rounds_and_respects_fixed_entries():
    run_eligibility_check("""
        events.find(event => event.id === 'open').openRoutes = ['MAIN', 'QUAL'];
        context.opportunitiesPointFinishes = (_event, route) => route === 'QUAL'
            ? [{value:'QR1',points:0},{value:'R32',points:2},{value:'QF',points:13}]
            : [{value:'R32',points:1},{value:'QF',points:11}];
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        click('select', 'open');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~R32~MAIN'));
        handlers.change({target:{dataset:{finish:'open'},value:'QF'}});
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QF~MAIN'));
        click('qualy', 'open');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QF~QUAL'));
        assert(root.innerHTML.includes('value="QR1"'));
        assert(root.innerHTML.includes('QF · 13 pts'));
        handlers.change({target:{dataset:{finish:'open'},value:'QR1'}});
        click('qualy', 'open');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~R32~MAIN'));
        assert(!root.innerHTML.includes('value="QR1"'));
        click('clear-schedule', '');
        click('select', 'mine');
        click('qualy', 'mine');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~MAIN'));
        assert(!root.innerHTML.includes('data-action="qualy"'));
        const rendered=root.innerHTML.replace(/<[^>]*>/g,'');
        assert(!rendered.includes('Assumed access'));
        assert(!rendered.includes('Assumed finish'));
        click('entries','mine');
        assert(root.innerHTML.includes('No entry list available yet.'));
        click('entries','mine');
        assert(!root.innerHTML.includes('op-entry-preview'));
        click('clear-schedule', '');
        events.find(event => event.id === 'alternate').entries[0].type = 'QUAL';
        click('select', 'alternate');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~R32~MAIN'));
        assert(!root.innerHTML.includes('value="QR1"'));
        click('qualy', 'alternate');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~R32~QUAL'));
        assert(root.innerHTML.includes('value="QR1"'));
        click('qualy', 'alternate');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~R32~MAIN'));
        assert(!root.innerHTML.includes('value="QR1"'));
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player',
            schedule:'alternate',results:'alternate~QF~MAIN'}));
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QF~MAIN'));
        click('qualy', 'alternate');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~QF~QUAL'));
    """)


@pytest.mark.parametrize("round_code", ["R32", "R64", "R128"])
def test_new_selection_starts_at_the_first_main_draw_round(round_code):
    run_eligibility_check("""
        const firstRound=""" + json.dumps(round_code) + """;
        context.opportunitiesPointFinishes=()=>[
            {value:'QR1',points:0},{value:firstRound,points:1},{value:'QF',points:11}];
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        click('select','open');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~'+firstRound+'~MAIN'));
        click('qualy','open');
        assert(context.collectOpportunitiesUrlState().results[0].endsWith('~'+firstRound+'~QUAL'));
    """)


def test_inline_entry_table_uses_shared_rows_and_escapes_provider_names():
    run_eligibility_check("""
        context.tournamentData.test=[
            {name:'<script>alert(1)</script>',country:'ARG',type:'MAIN',pos:'1',pos_num:1,seed:1,rank:'10',priority:'1'},
            {name:'Qualifier',type:'QUAL',pos:'1',pos_num:1,rank:'100',priority:'2'},
            {name:'Alternate',type:'ALT',pos:'1',pos_num:1,rank:'200',priority:'3'},
        ];
        const html=context.entryListTableMarkup('test');
        assert(html.includes('&lt;SCRIPT&gt;ALERT(1)&lt;/SCRIPT&gt;'));
        assert(!html.toLowerCase().includes('<script>'));
        assert(html.includes('entry-seed-col'));
        assert(html.includes('>S</th>'));
        assert(html.includes('PRIO'));
        assert(html.includes('E-Rank'));
        assert(html.indexOf('QUALIFYING')<html.indexOf('QUALIFIER'));
        assert(html.indexOf('QUALIFIER')<html.indexOf('ALTERNATES'));
        assert(!html.includes('entry-wtn-col'));
    """)


def test_shared_schedule_keeps_only_one_choice_per_week_and_preserves_other_weeks():
    run_eligibility_check("""
        events.find(event => event.id === 'alternate').startDate = '2026-11-09';
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player',
            starting:'2026-11-02', ending:'2026-11-09', schedule:'mine,open,alternate'}));
        assert.deepEqual(Array.from(context.collectOpportunitiesUrlState().schedule), ['mine', 'alternate']);
        click('select', 'mine');
        assert.deepEqual(Array.from(context.collectOpportunitiesUrlState().schedule), ['alternate']);
        assert(root.innerHTML.includes('data-event="open"'));
        click('select', 'open');
        assert.deepEqual(Array.from(context.collectOpportunitiesUrlState().schedule), ['alternate', 'open']);
        click('clear-schedule', '');
        assert.equal(context.collectOpportunitiesUrlState().schedule, undefined);
    """)


def test_schedule_detects_overlapping_and_same_day_events():
    run_eligibility_check("""
        const first = {...common, name: 'First', startDate: '2026-11-02', endDate: '2026-11-08'};
        const overlaps = {...common, name: 'Overlap', startDate: '2026-11-08', endDate: '2026-11-15'};
        const following = {...common, name: 'Following', startDate: '2026-11-09', endDate: '2026-11-15'};
        assert.equal(context.opportunitiesScheduleConflicts([first, following]).length, 0);
        const conflicts = context.opportunitiesScheduleConflicts([first, overlaps]);
        assert.equal(conflicts.length, 1);
        assert.deepEqual(Array.from(conflicts[0]), ['First', 'Overlap']);
    """)


def test_ending_weeks_merge_shared_slam_deadlines_and_preserve_the_points_cutoff():
    run_eligibility_check(r"""
        context.WTARG_DATA.opportunities.gsDeadlines = [{id:'ao', name:'Australian Open', year:2027,
            mdCutoff:'2026-11-15', qCutoff:'2026-11-15'}];
        events.find(event => event.id === 'mine').startDate = '2026-11-09';
        events.find(event => event.id === 'alternate').startDate = '2026-11-16';
        let horizon;
        context.opportunitiesScheduleScenario = (_player,_selections,target) => {
            horizon=target; return {baseline:200,gain:0,total:200,replaced:[]};
        };
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player',
            starting:'2026-11-02', ending:'gs-ao-2027-q'}));
        const endingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert(!endingHtml.includes('optgroup'));
        assert.equal((endingHtml.match(/AO&#39;27/g)||[]).length,1);
        assert(endingHtml.includes('value="2026-11-09" selected>AO&#39;27 · 09/11/2026'));
        assert(endingHtml.includes('value="2026-11-16">16/11/2026'));
        assert(endingHtml.includes('value="2027-03-01"'));
        assert(!endingHtml.includes('value="2027-03-08"'));
        assert.equal(horizon,'2026-11-15');
        assert.equal(context.collectOpportunitiesUrlState().ending,'2026-11-09');
        assert.equal(context.opportunitiesSlamOptions()[0].deadlineLabel,"AO'27");
        assert(root.innerHTML.includes('data-event="mine"'));
        assert(!root.innerHTML.includes('data-event="alternate"'));
        handlers.change({target:{dataset:{filter:'ending'},value:'2026-11-23'}});
        assert(root.innerHTML.includes('data-event="alternate"'));
        assert.equal(horizon,'2026-11-30');
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-11-16'}});
        assert(!root.innerHTML.includes('AO&#39;27'));
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-10-19'}});
        const fullEndingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert.equal((fullEndingHtml.match(/<option /g)||[]).length,20);
    """)


def test_ending_weeks_extend_through_the_next_qualifying_deadline_with_distinct_draws():
    run_eligibility_check(r"""
        context.WTARG_DATA.opportunities.gsDeadlines = [{id:'rg',name:'Roland Garros',year:2027,
            mdCutoff:'2027-04-12',qCutoff:'2027-04-26'},
            {id:'wim',name:'Wimbledon',year:2027,mdCutoff:'2027-05-17',qCutoff:'2027-05-31'},
            {id:'unknown',name:'Missing dates',year:2027,mdCutoff:'N/A',qCutoff:null}];
        await context.restoreOpportunitiesUrlState(new URLSearchParams({player:'test-player'}));
        const endingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert(endingHtml.includes('value="2027-04-05">RG&#39;27 MD · 05/04/2027'));
        assert(endingHtml.includes('value="2027-04-19">RG&#39;27 Q · 19/04/2027'));
        assert(!endingHtml.includes('value="2027-04-26"'));
        assert(!endingHtml.includes('WMB'));
        assert(!endingHtml.includes('Missing dates'));
        assert.deepEqual(Array.from(context.opportunitiesSlamOptions(), item => item.deadlineLabel),
            ["RG'27 MD", "RG'27 Q", "WMB'27 MD", "WMB'27 Q"]);
    """)


def test_shared_url_writer_preserves_planner_identifiers_and_round_choices():
    script = r"""
        const vm = require('vm');
        const fs = require('fs');
        const assert = require('assert/strict');
        const source = fs.readFileSync(0, 'utf8');
        const location = {origin: 'https://example.test', protocol: 'https:',
            pathname: '/', search: '', href: 'https://example.test/'};
        const context = {WTARG_DATA: {tournaments: []}, SITE_BASE_PATH: '/', location,
            URL, URLSearchParams, console, history: {replaceState(_state, _unused, target) {
                const next = new URL(target, location.origin);
                Object.assign(location, {pathname: next.pathname, search: next.search, href: next.href});
            }}};
        context.window = context;
        vm.createContext(context);
        vm.runInContext(source.slice(0, source.indexOf('function trackCurrentUrlState()')), context);
        const first = 'itf:W-ITF-ARG-2026-001';
        const second = 'wta:125:2026:BUENOS_AIRES';
        const results = [first + '~QF~QUAL', second + '~R16~MAIN'];
        context.writeUrlStateForTab('opportunities', {player: 'Test Player',
            compare: [first], schedule: [first, second], results}, {track: false});
        const params = new URLSearchParams(location.search);
        assert.equal(location.pathname, '/opportunities/');
        assert.equal(params.get('player'), 'test-player');
        assert.equal(params.get('compare'), first);
        assert.equal(params.get('schedule'), [first, second].join(','));
        assert.equal(params.get('results'), results.join(','));
        const direct = new URLSearchParams();
        context.setStateParam(direct, 'results', results, {raw: true});
        assert.equal(direct.get('results'), results.join(','));
    """
    completed = subprocess.run(
        [NODE, "-e", script],
        input=(PROJECT_ROOT / "web/js/app.js").read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
