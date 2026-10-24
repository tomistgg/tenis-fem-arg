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
                planner: {asOf: '2026-10-12', players: [{id: 'player-1', name: 'Test Player', points: 1710}]}}};
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
        const scenario = (...args) => context.plannerPointScenario('Test Player', ...args);
        const schedule = (...args) => context.plannerScheduleScenario('Test Player', ...args);
        const finish = (round, route) => context.plannerPointFinishes(event, route)
            .find(item => item.value === round);
    """ + check
    payload = {
        "ledger": (PROJECT_ROOT / "web/js/tabs/roadtogs.js").read_text(encoding="utf-8"),
        "points": (PROJECT_ROOT / "web/js/tabs/planner-points.js").read_text(encoding="utf-8"),
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


def test_projected_breakdown_moves_displaced_results_without_mutating_the_baseline():
    run_points_check("""
        const value = scenario(event, 'SF');
        assert.equal(value.breakdown.totalPoints, value.total);
        assert.equal(value.breakdown.countable.reduce((sum, row) => sum + row.points, 0), value.total);
        assert.equal(value.breakdown.countable.length, 18);
        const planned = value.breakdown.countable.find(row => row._plannerId === event.id);
        assert.equal(planned.roundDisplay, 'SF');
        assert.equal(planned.dropDate, '2027-10-25');
        const displaced = value.breakdown.nonCountable.find(row => row.tournamentId === 'past-18');
        assert.equal(displaced.points, 10);
        assert.equal(displaced.replacedBy._plannerId, event.id);
        assert(baseline.every(row => !row.replacedBy));
        const reset = schedule([], '2026-11-02');
        assert.equal(reset.breakdown.countable.length, 18);
        assert.equal(reset.breakdown.totalPoints, 1710);
        assert.equal(reset.breakdown.nonCountable.length, 0);
        assert(reset.breakdown.countable.every(row => !row._planner));
    """)


def test_projected_breakdown_resolves_historical_and_planned_replacement_chains():
    run_points_check("""
        const second = {...event, id: 'second', tournamentId: 'second-id', name: 'Second Tournament',
            startDate: '2026-10-26', endDate: '2026-11-01'};
        const value = schedule([{event, finish: 'SF'}, {event: second, finish: 'W'}], '2026-11-02');
        const historical = value.breakdown.nonCountable.find(row => row.tournamentId === 'past-18');
        const planned = value.breakdown.nonCountable.find(row => row._plannerId === event.id);
        assert.equal(historical.replacedBy._plannerId, second.id);
        assert.equal(planned.replacedBy._plannerId, second.id);
        assert.equal(value.breakdown.countable.reduce((sum, row) => sum + row.points, 0), value.total);
        assert.equal(new Set(value.breakdown.nonCountable.map(row => row.tournamentId)).size, 2);
    """)


def test_ao_cutoffs_include_published_points_with_w15_w35_delay():
    run_points_check("""
        context.WTARG_DATA.planner.gsDeadlines = [{name: 'Australian Open',
            mdCutoff: '2026-12-07', qCutoff: '2026-12-21'}];
        for (const [cutoff, start, end, earlierStart, earlierEnd] of [
            ['2026-12-07', '2026-11-30', '2026-12-06', '2026-11-23', '2026-11-29'],
            ['2026-12-21', '2026-12-14', '2026-12-20', '2026-12-07', '2026-12-13']]) {
            const finalWeek = {...event, startDate: start, endDate: end};
            assert.equal(scenario(finalWeek, 'SF', cutoff).gross, 20);
            const delayed = {...finalWeek, level: 'W35'};
            assert.equal(scenario(delayed, 'SF', cutoff).gross, 0);
            const earlier = {...delayed, startDate: earlierStart, endDate: earlierEnd};
            assert.equal(scenario(earlier, 'SF', cutoff).gross, 20);
            assert.equal(scenario(finalWeek, 'SF', finalWeek.endDate).gross, 0);
        }
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
        const gsFinishes = context.plannerPointFinishes(grandSlam, 'QUAL');
        assert.equal(gsFinishes.find(item => item.value === 'R128').points, 40);
        assert.equal(gsFinishes.find(item => item.value === 'R32').points, 170);
        const lowerTier = {...event, level: 'W35', pointsDescription: 'W35 (32M, 64/48/32/24Q)'};
        const md = context.plannerPointFinishes(lowerTier, 'MAIN');
        const qualifying = context.plannerPointFinishes(lowerTier, 'QUAL');
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
        assert.equal(context.plannerPointDate(event), '2026-10-26');
        assert.equal(context.plannerPointDate({...event, level: 'W15'}), '2026-11-02');
        assert.equal(context.plannerPointDate({...event, level: 'W35'}), '2026-11-02');
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
        assert.equal(value.breakdown.nonCountable.length, 1);
        assert.equal(value.breakdown.nonCountable[0].points, 6);
        assert.equal(value.breakdown.nonCountable[0].replacedBy._plannerId, event.id);
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
        assert.equal(value.breakdown.countable.length, 1);
        assert.equal(value.breakdown.countable[0].points, 6);
        assert.equal(value.breakdown.nonCountable.length, 0);
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
        context.WTARG_DATA.planner.players[0].points = 1800;
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
        assert.equal(context.plannerPointDate({level:'W15',endDate:'2026-10-11'}),'2026-10-19');
    """)


def test_current_week_progress_counts_without_selecting_the_event_and_only_adds_improvement():
    run_points_check("""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        const live = {...event, progress: {'player-1': {round:'QF',route:'MAIN',completed:false}}};
        context.WTARG_DATA.planner.events = [live];
        const earned = schedule([], '2026-10-26');
        assert.equal(earned.baseline,1711);
        assert.equal(earned.total,1711);
        assert.equal(earned.gain,0);
        assert.equal(earned.currentTotal,1710);
        const unchanged = scenario(live,'QF','2026-10-26');
        assert.equal(unchanged.baseline,1711);
        assert.equal(unchanged.gain,0);
        assert.equal(unchanged.total,1711);
        const title = scenario(live,'W','2026-10-26');
        assert.equal(title.baseline,1711);
        assert.equal(title.total,1750);
        assert.equal(title.gain,39);
        assert.equal(title.breakdown.countable.filter(row=>row.tournamentId===live.tournamentId).length,1);
        assert.equal(title.replaced.length,1);
        assert.equal(title.replaced[0].points,11);
    """)


def test_current_week_progress_replaces_stale_history_and_preserves_an_equal_recorded_result():
    run_points_check("""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        const recorded = {...result(6,1), tournament:event.name,
            tournamentId:event.tournamentId, date:event.startDate};
        baseline = [result(100,0),recorded];
        const live = {...event, progress: {'player-1': {round:'QF',route:'MAIN',completed:false}}};
        context.WTARG_DATA.planner.events = [live];
        const earned = schedule([], '2026-10-26');
        assert.equal(earned.baseline,111);
        assert.equal(earned.total,111);
        assert.equal(earned.breakdown.countable.filter(row=>row.tournamentId===live.tournamentId).length,1);
        assert.equal(recorded.points,6);
        assert(!recorded.replacedBy);
        recorded.points = 11;
        const equal = schedule([], '2026-11-02');
        assert.equal(equal.total,111);
        const retained = equal.breakdown.countable.find(row=>row.tournamentId===live.tournamentId);
        assert.equal(retained.tournament,recorded.tournament);
        assert.equal(retained.roundDisplay,recorded.roundDisplay);
        assert(!retained._planner);
        assert.equal(equal.breakdown.nonCountable.length,0);
    """)


def test_current_week_progress_for_other_players_does_not_change_the_baseline():
    run_points_check("""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        context.WTARG_DATA.planner.events = [{...event,
            progress: {'another-player': {round:'W',route:'MAIN',completed:true}}}];
        const value = schedule([], '2026-10-26');
        assert.equal(value.baseline,1710);
        assert.equal(value.total,1710);
        assert.equal(value.breakdown.countable.length,18);
    """)


def test_current_week_earned_w35_points_wait_until_publication_and_expire():
    run_points_check("""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        const live = {...event, level:'W35',
            progress: {'player-1': {round:'SF',route:'MAIN',completed:false}}};
        context.WTARG_DATA.planner.events = [live];
        assert.equal(schedule([], '2026-10-26').baseline,1710);
        assert.equal(schedule([], '2026-11-02').baseline,1720);
        assert.equal(schedule([], '2027-11-01').baseline,1710);
    """)


def test_live_finishes_start_at_the_current_round_and_completed_results_are_fixed():
    run_points_check("""
        const live = {...event,currentProgress:{round:'QF',route:'MAIN',completed:false}};
        assert.deepEqual(Array.from(context.plannerPointFinishes(live,'MAIN'),row=>row.value),
            ['QF','SF','F','W']);
        const completed = {...live,currentProgress:{round:'QF',route:'MAIN',completed:true}};
        assert.deepEqual(Array.from(context.plannerPointFinishes(completed,'MAIN'),row=>row.value),['QF']);
        const champion = {...live,currentProgress:{round:'W',route:'MAIN',completed:true}};
        assert.deepEqual(Array.from(context.plannerPointFinishes(champion,'MAIN'),row=>row.value),['W']);
        const qualifying = {...event,currentProgress:{round:'QR2',route:'QUAL',completed:false}};
        const choices = Array.from(context.plannerPointFinishes(qualifying,'QUAL'),row=>row.value);
        assert.equal(choices[0],'QR2');
        assert(!choices.includes('QR1'));
        assert.equal(choices.at(-1),'W');
    """)


def test_lucky_loser_progress_keeps_the_qualifying_loss_award_without_a_qualifier_bonus():
    run_points_check("""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        baseline = [result(100,0)];
        const progress = {round:'R16',route:'QUAL',qualifyingRound:'QR2',completed:false};
        const live = {...event,assumedRoute:'QUAL',progress:{'player-1':progress},currentProgress:progress};
        context.WTARG_DATA.planner.events = [live];
        const choices = context.plannerPointFinishes(live,'QUAL');
        assert.equal(choices.find(row=>row.value==='R16').points,7);
        assert.equal(choices.find(row=>row.value==='W').points,51);
        const earned = schedule([], '2026-10-26');
        assert.equal(earned.baseline,107);
        const title = scenario(live,'W','2026-10-26');
        assert.equal(title.total,151);
        assert.equal(title.gain,44);
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
            {...common, id: 'past', startDate: '2026-10-05', endDate: '2026-10-11', entryPublished: true,
                entries: [{playerKey: 'player-1', type: 'MAIN'}]},
        ];
        const handlers = {};
        const root = {innerHTML: '', contains: () => false,
            addEventListener(type, callback) { handlers[type] = callback; }};
        const context = {document: {getElementById: () => root, querySelector: () => null,
            addEventListener() {}}, console,
            WTARG_DATA: {planner: {asOf: '2026-10-18', events,
                players: [{id: 'player-1', name: 'Test Player', rank: 300, points: 200}]}},
            ensureHistoryDataLoaded: async () => {},
            plannerPointFinishes: () => [{value: 'QF', label: 'Quarterfinal', points: 11}],
            plannerPointDate: () => '2026-11-09',
            plannerPointScenario: () => ({gross: 11, gain: 5, replaced: []}),
            tournamentData:{}, countryFlag:()=>'', getDisplayName:value=>value};
        context.window = context;
        vm.createContext(context);
        vm.runInContext(input.formatter, context);
        vm.runInContext(input.entries, context);
        vm.runInContext(input.ledger, context);
        vm.runInContext(input.frontend, context);
        const eligible = () => Array.from(context.plannerEligibleEvents(events,
            'player-1', '2026-10-18'), event => event.id);
        const click = (action, id) => handlers.click({target: {closest: () => ({dataset: {action, event: id}})}});
        (async () => {
    """ + check + "\n})().catch(error => {console.error(error); process.exitCode = 1;});"
    app = (PROJECT_ROOT / "web/js/app.js").read_text(encoding="utf-8")
    payload = {
        "frontend": (PROJECT_ROOT / "web/js/tabs/planner.js").read_text(encoding="utf-8"),
        "points": (PROJECT_ROOT / "web/js/tabs/planner-points.js").read_text(encoding="utf-8"),
        "ledger": (PROJECT_ROOT / "web/js/tabs/roadtogs.js").read_text(encoding="utf-8"),
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
        await context.initPlanner();
        assert.equal(context.collectPlannerUrlState().player,'');
        assert(root.innerHTML.includes('value="" selected>Select Player</option>'));
        assert(!root.innerHTML.includes('data-action="select"'));
        click('select','mine');
        assert(!context.collectPlannerUrlState().schedule);
        handlers.change({target:{dataset:{filter:'player'},value:'player-1'}});
        assert.equal(context.collectPlannerUrlState().player,'test-player');
        click('select','mine');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['mine']);
        handlers.change({target:{dataset:{filter:'player'},value:''}});
        assert.equal(context.collectPlannerUrlState().player,'');
        assert(!context.collectPlannerUrlState().schedule);
        assert(!root.innerHTML.includes('data-action="select"'));
    """)


def test_touch_selections_do_not_refocus_rebuilt_pickers_but_keyboard_selections_do():
    run_eligibility_check("""
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        let focusCount=0;
        root.contains=()=>true;
        root.focus=()=>{focusCount++;};
        context.document.activeElement={id:'op-player'};
        handlers.pointerdown?.({pointerType:'touch'});
        handlers.change({target:{dataset:{filter:'player'},value:'player-1'}});
        assert.equal(focusCount,0,'Touch selection must not refocus the native player picker');
        context.document.activeElement={id:'op-ending'};
        handlers.change({target:{dataset:{filter:'ending'},value:'2026-11-09'}});
        assert.equal(focusCount,0,'Touch selection must not refocus the week picker');
        handlers.keydown?.({key:'ArrowDown'});
        handlers.change({target:{dataset:{filter:'ending'},value:'2026-11-16'}});
        assert.equal(focusCount,1,'Keyboard users must retain focus after selection');
        handlers.pointerdown?.({pointerType:'mouse'});
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-11-02'}});
        assert.equal(focusCount,2,'Mouse users retain the existing focus behavior');
    """)


def test_all_future_tournaments_allow_wildcards_and_default_to_main_draw():
    run_eligibility_check("""
        assert.deepEqual(eligible(), ['mine','alternate','absent','open','closed',
            'deadline-today','unknown','no-route']);
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        assert(!root.innerHTML.includes('op-entry-uncertain'));
        assert(root.innerHTML.includes('Conditional on acceptance or a wildcard.'));
        for (const id of eligible()) {
            click('select',id);
            assert(context.collectPlannerUrlState().results[0].endsWith('~MAIN'));
            if (id !== 'mine') {
                click('qualy',id);
                assert(context.collectPlannerUrlState().results[0].endsWith('~QUAL'));
                click('qualy',id);
                assert(context.collectPlannerUrlState().results[0].endsWith('~MAIN'));
            }
            click('clear-schedule','');
        }
    """)


def test_current_week_is_selected_and_an_ongoing_event_restores_only_possible_results():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        context.WTARG_DATA.pointsDistribution = [{Description:'W50 (32M, 32Q)',
            W:50,F:33,SF:20,QF:11,R16:6,R32:1,QLFR:2,QR2:1}];
        vm.runInContext(input.points,context);
        context.plannerScheduleScenario = () => null;
        context.plannerPointScenario = () => ({gain:0,replaced:[]});
        const current = events.find(event=>event.id==='mine');
        Object.assign(current,{startDate:'2026-10-12',endDate:'2026-10-20',progress:{
            'player-1':{round:'QF',route:'MAIN',completed:false,played:true},
            'other-player':{round:'W',route:'MAIN',completed:true,played:true}}});
        const finished = events.find(event=>event.id==='alternate');
        Object.assign(finished,{startDate:'2026-10-19',endDate:'2026-10-20'});
        const included = Array.from(context.plannerEligibleEvents(events,'player-1','2026-10-21'),row=>row.id);
        assert(included.includes('mine'));
        assert(!included.includes('alternate'));
        assert(!included.includes('past'));
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
            starting:'2026-10-12',schedule:'mine',results:'mine~R32~QUAL'}));
        const saved = context.collectPlannerUrlState();
        assert.equal(saved.starting,'2026-10-19');
        assert.deepEqual(Array.from(saved.schedule),['mine']);
        assert.deepEqual(Array.from(saved.results),['mine~QF~MAIN']);
        const choices = root.innerHTML.match(/<select id="op-choice-finish-mine"[^>]*>(.*?)<\/select>/)[1];
        assert(choices.includes('value="QF" selected'));
        assert(choices.includes('value="W"'));
        assert(!choices.includes('value="R32"'));
        assert(!choices.includes('value="R16"'));
        assert(!choices.includes('value="QR1"'));
    """)


def test_known_singles_participation_locks_its_week_even_before_play_or_after_elimination():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        const current = events.find(event=>event.id==='mine');
        Object.assign(current,{startDate:'2026-10-19',endDate:'2026-10-25'});
        const alternative = events.find(event=>event.id==='alternate');
        Object.assign(alternative,{startDate:'2026-10-19',endDate:'2026-10-25'});
        for (const progress of [
            {round:'QF',route:'MAIN',completed:false,played:true},
            {round:'R32',route:'MAIN',completed:false,played:false},
            {round:'QF',route:'MAIN',completed:true,played:true}
        ]) {
            current.progress = {'player-1':progress};
            await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
                schedule:'alternate,rest:2026-10-19',compare:'alternate'}));
            let saved = context.collectPlannerUrlState();
            assert.deepEqual(Array.from(saved.schedule),['mine']);
            assert.equal(saved.compare,undefined);
            assert(!root.innerHTML.includes('data-event="alternate"'));
            assert(!root.innerHTML.includes('data-event="rest:2026-10-19"'));
            const button = root.innerHTML.match(/<button[^>]*id="op-pick-mine"[^>]*>/)[0];
            assert(button.includes(' disabled'));
            assert(button.includes('title="Tournament fixed for this week"'));
            assert(root.innerHTML.includes('op-choice-finish-mine'));
            for (const [action,id] of [['select','mine'],['schedule','alternate'],
                ['select','rest:2026-10-19'],['compare','alternate']]) {
                click(action,id);
                saved = context.collectPlannerUrlState();
                assert.deepEqual(Array.from(saved.schedule),['mine']);
                assert.equal(saved.compare,undefined);
            }
            click('clear-schedule','');
            assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['mine']);
        }
    """)


def test_week_lock_preserves_finish_choices_and_updates_with_player_and_date_range():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        context.WTARG_DATA.pointsDistribution = [{Description:'W50 (32M, 32Q)',
            W:50,F:33,SF:20,QF:11,R16:6,R32:1,QLFR:2,QR2:1}];
        vm.runInContext(input.points,context);
        context.plannerScheduleScenario = () => null;
        context.plannerPointScenario = () => ({gain:0,replaced:[]});
        const current = events.find(event=>event.id==='mine');
        Object.assign(current,{startDate:'2026-10-19',endDate:'2026-10-25',progress:{
            'player-1':{round:'QF',route:'MAIN',completed:false,played:true}}});
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        assert.deepEqual(Array.from(context.collectPlannerUrlState().results),['mine~QF~MAIN']);
        handlers.change({target:{dataset:{finish:'mine'},value:'W'}});
        assert.deepEqual(Array.from(context.collectPlannerUrlState().results),['mine~W~MAIN']);
        click('select','open');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['mine','open']);
        click('clear-schedule','');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['mine']);
        assert.deepEqual(Array.from(context.collectPlannerUrlState().results),['mine~W~MAIN']);
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-11-02'}});
        assert.equal(context.collectPlannerUrlState().schedule,undefined);
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-10-19'}});
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['mine']);
        context.WTARG_DATA.planner.players.push({id:'player-2',name:'Other Player'});
        handlers.change({target:{dataset:{filter:'player'},value:'player-2'}});
        assert.equal(context.collectPlannerUrlState().schedule,undefined);
        assert(root.innerHTML.includes('data-event="rest:2026-10-19"'));
        click('select','rest:2026-10-19');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['rest:2026-10-19']);
        handlers.change({target:{dataset:{filter:'player'},value:'player-1'}});
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule),['mine']);
    """)


def test_current_qualifying_round_defaults_before_main_draw_and_locks_the_played_route():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.asOf = '2026-10-21';
        context.WTARG_DATA.pointsDistribution = [{Description:'W50 (32M, 32Q)',
            W:50,F:33,SF:20,QF:11,R16:6,R32:1,QLFR:2,QR2:1}];
        vm.runInContext(input.points,context);
        context.plannerScheduleScenario = () => null;
        context.plannerPointScenario = () => ({gain:0,replaced:[]});
        const current = events.find(event=>event.id==='open');
        Object.assign(current,{startDate:'2026-10-19',endDate:'2026-10-25',progress:{
            'player-1':{round:'QR2',route:'QUAL',completed:false,played:true}}});
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
            schedule:'open',results:'open~QR1~MAIN'}));
        assert.deepEqual(Array.from(context.collectPlannerUrlState().results),['open~QR2~QUAL']);
        click('qualy','open');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().results),['open~QR2~QUAL']);
        const choices = root.innerHTML.match(/<select id="op-choice-finish-open"[^>]*>(.*?)<\/select>/)[1];
        assert(choices.includes('value="QR2" selected'));
        assert(choices.includes('value="W"'));
        assert(!choices.includes('value="QR1"'));
    """)


def test_existing_wta_entry_does_not_hide_itf_tournaments_with_open_deadlines():
    run_eligibility_check("""
        events.find(event => event.id === 'mine').source = 'WTA';
        events.push({...common, id: 'both-open', source: 'WTA', openRoutes: ['MAIN', 'QUAL']},
            {...common, id: 'itf-open', openRoutes: ['MAIN', 'QUAL']},
            {...common, id: 'main-only', source: 'WTA', openRoutes: ['MAIN']},
            {...common, id: 'unlocked', startDate: '2026-11-09', openRoutes: ['MAIN', 'QUAL']});
        const routes = id => Array.from(context.plannerEntryRoutes(
            events.find(event => event.id === id), 'player-1'));
        assert.deepEqual(routes('mine'), ['MAIN']);
        assert.deepEqual(routes('both-open'), ['MAIN', 'QUAL']);
        assert.deepEqual(routes('itf-open'), ['MAIN', 'QUAL']);
        assert.deepEqual(routes('main-only'), ['MAIN', 'QUAL']);
        assert.deepEqual(routes('unlocked'), ['MAIN', 'QUAL']);
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
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
        assert.deepEqual(Array.from(context.plannerEntryRoutes(event, 'player-1')), ['MAIN','QUAL']);
        event.deadlineQual = '2026-10-18';
        event.deadlineMain = '2026-10-01';
        event.openRoutes = [];
        assert.deepEqual(Array.from(context.plannerEntryRoutes(event, 'player-1')), ['MAIN','QUAL']);
        Object.assign(events.find(e=>e.id==='alternate'),event,{id:'alternate',entries:[]});
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        click('select','alternate');
        assert(context.collectPlannerUrlState().results[0].endsWith('~MAIN'));
        click('qualy','alternate');
        assert(context.collectPlannerUrlState().results[0].endsWith('~QUAL'));
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
            schedule:'alternate',results:'alternate~QF~QUAL'}));
        assert(context.collectPlannerUrlState().results[0].endsWith('~QUAL'));
    """)


def test_delay_markers_appear_only_for_w15_w35_in_w50_cutoff_weeks():
    run_eligibility_check("""
        context.WTARG_DATA.planner.w50CutoffWeeks = ['2026-11-09'];
        context.WTARG_DATA.planner.w50CutoffDraws = {
            '2026-11-09': [{name:'Roland Garros',draw:''}]};
        events.push({...common, id: 'earlier', level: 'W35',
                entries: [{playerKey: 'player-1', type: 'MAIN', position: 8}]},
            {...common, id: 'delayed', level: 'W35', startDate: '2026-11-09'},
            {...common, id: 'on-time', level: 'W50', startDate: '2026-11-09'});
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        const button = id => root.innerHTML.match(new RegExp('<button[^>]*data-event="' + id + '".*?</button>'))[0];
        assert(button('delayed').includes('op-delay-badge'));
        assert(button('delayed').includes('>!</span>'));
        assert(!button('earlier').includes('op-delay-badge'));
        assert(!button('on-time').includes('op-delay-badge'));
        assert(!root.innerHTML.includes('+1w'));
        assert(!root.innerHTML.includes('Main draw #8'));
        assert(!root.innerHTML.includes('op-calendar-entry'));
        assert(root.innerHTML.includes('points not added before RG deadline'));
        assert(root.innerHTML.indexOf('op-week-note') < root.innerHTML.lastIndexOf('</h3>'));
        context.WTARG_DATA.planner.w50CutoffDraws['2026-11-09'] = [{name:'Roland Garros',draw:'Q'}];
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
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
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
        await context.restorePlannerUrlState(query);
        const state = context.collectPlannerUrlState();
        assert.deepEqual(Array.from(state.compare), ['mine']);
        assert.deepEqual(Array.from(state.schedule), ['mine']);
        assert.deepEqual(Array.from(state.results), ['mine~QF~MAIN']);
    """)


def test_calendar_clicks_toggle_the_schedule_and_restore_other_options_in_that_week():
    run_eligibility_check("""
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        click('select', 'mine');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['mine']);
        assert(!root.innerHTML.includes('data-event="open"'));
        assert(!root.innerHTML.includes('data-event="alternate"'));
        assert(root.innerHTML.includes('aria-pressed="true"'));
        assert(root.innerHTML.indexOf('op-summary') < root.innerHTML.indexOf('op-results'));
        assert(root.innerHTML.includes('op-choice-finish-mine'));
        assert(!root.innerHTML.includes('op-schedule-events'));
        click('select', 'mine');
        assert.equal(context.collectPlannerUrlState().schedule, undefined);
        assert(!root.innerHTML.includes('op-choice-finish-mine'));
        assert(root.innerHTML.includes('data-event="open"'));
        click('select', 'open');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['open']);
        click('schedule', 'open');
        assert.equal(context.collectPlannerUrlState().schedule, undefined);
        assert(root.innerHTML.includes('data-event="mine"'));
    """)


def test_rest_reserves_a_week_without_adding_results_or_tournament_controls():
    run_eligibility_check("""
        let scored = [];
        context.plannerScheduleScenario = (player, selections) => {
            scored = selections;
            return {baseline: 200, gain: selections.length * 11, total: 200 + selections.length * 11};
        };
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        const rest = 'rest:2026-11-02';
        assert(root.innerHTML.includes('cal-rest'));
        click('select', rest);
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), [rest]);
        assert.equal(context.collectPlannerUrlState().results, undefined);
        assert.equal(scored.length, 0);
        assert(!root.innerHTML.includes('data-event="mine"'));
        assert(!root.innerHTML.includes('op-inline-controls'));
        assert(!root.innerHTML.includes('Point table unavailable'));
        click('select', rest);
        assert.equal(context.collectPlannerUrlState().schedule, undefined);
        assert(root.innerHTML.includes('data-event="mine"'));
        click('select', 'mine');
        assert.equal(scored.length, 1);
        assert.equal(scored[0].event.id, 'mine');
        assert(!root.innerHTML.includes('data-event="' + rest + '"'));
        click('clear-schedule', '');
        assert(root.innerHTML.includes('data-event="' + rest + '"'));
    """)


def test_rest_covers_empty_weeks_and_survives_shared_tour_restore():
    run_eligibility_check("""
        let scored = [];
        context.plannerScheduleScenario = (player, selections) => { scored = selections; return null; };
        await context.restorePlannerUrlState(new URLSearchParams({player: 'test-player',
            starting: '2026-11-02', ending: '2026-11-16'}));
        assert.equal((root.innerHTML.match(/class="op-week op-panel"/g) || []).length, 3);
        const rest = 'rest:2026-11-09';
        assert(root.innerHTML.includes('data-event="' + rest + '"'));
        click('select', 'mine');
        click('select', rest);
        const saved = context.collectPlannerUrlState();
        assert.deepEqual(Array.from(saved.schedule), ['mine', rest]);
        assert.equal(saved.results.length, 1);
        assert.equal(scored.length, 1);
        const params = new URLSearchParams(saved);
        click('clear-schedule', '');
        await context.restorePlannerUrlState(params);
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['mine', rest]);
        assert.equal(scored.length, 1);
        handlers.change({target: {dataset: {filter: 'ending'}, value: '2026-11-02'}});
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['mine']);
        assert(!root.innerHTML.includes('data-event="' + rest + '"'));
    """)


def test_planner_breakdown_updates_below_the_calendar_with_escaped_names_and_result_labels():
    run_eligibility_check("""
        context.plannerPointFinishes = () => [
            {value: 'QF', label: 'Quarterfinal', points: 11}, {value: 'SF', label: 'Semifinal', points: 20}];
        const original = {tournament: 'W35 Past Event', matchType: 'ITF', category: 'W35',
            date: '2026-09-07', roundDisplay: 'QF', points: 8, dropDate: '2027-09-13'};
        context.plannerScheduleScenario = (player, selections) => {
            const future = {tournament: 'Future', displayName: 'W50 Future <script>alert(1)</script>',
                category: 'W50', _planner: true, date: '2026-11-02', roundDisplay: selections[0]?.finish,
                points: 11, dropDate: '2027-11-08'};
            return {baseline: 8, gain: selections.length ? 3 : 0, total: selections.length ? 11 : 8,
                breakdown: {countable: selections.length ? [future] : [original],
                    nonCountable: selections.length ? [{...original, replacedBy: future},
                        {...future, displayName: 'W50 Earlier', replacedBy: future}] : []}};
        };
        await context.restorePlannerUrlState(new URLSearchParams({player: 'test-player'}));
        assert(root.innerHTML.includes('id="op-breakdown-table"'));
        assert(root.innerHTML.indexOf('id="op-breakdown-table"') > root.innerHTML.indexOf('op-week-list'));
        assert(!root.innerHTML.includes('op-result-replaced'));
        click('select', 'mine');
        let table = root.innerHTML.split('id="op-breakdown-table"')[1];
        const separator = table.indexOf('NON-COUNTABLE TOURNAMENTS');
        assert(separator > 0);
        assert(table.indexOf('W35 Past Event') > separator);
        assert(table.indexOf('W50 Future') < separator);
        assert(table.includes('class="op-result-replaced"><span>Replaced</span>'));
        assert(table.includes('class="op-result-new"><span>New</span>'));
        const earlierRow = table.split('W50 Earlier')[1].split('</tr>')[0];
        assert(earlierRow.includes('op-result-new'));
        assert(!earlierRow.includes('op-result-replaced'));
        assert(table.includes('W50 Future &lt;script&gt;alert(1)&lt;/script&gt;'));
        assert(!table.includes('<script>'));
        assert(table.includes('<td>QF</td>'));
        handlers.change({target: {dataset: {finish: 'mine'}, value: 'SF'}});
        table = root.innerHTML.split('id="op-breakdown-table"')[1];
        assert(table.includes('<td>SF</td>'));
        click('clear-schedule', '');
        table = root.innerHTML.split('id="op-breakdown-table"')[1];
        assert(table.includes('W35 Past Event'));
        assert(!table.includes('W50 Future'));
        assert(!table.includes('op-result-replaced'));
        assert(!table.includes('op-result-new'));
        click('select', 'rest:2026-11-02');
        assert(!root.innerHTML.includes('op-result-replaced'));
        handlers.change({target: {dataset: {filter: 'player'}, value: ''}});
        assert(!root.innerHTML.includes('id="op-breakdown-table"'));
        assert(!original.replacedBy);
    """)


def test_displaced_results_use_shared_compact_tournament_names_and_point_units():
    run_eligibility_check("""
        const replaced = [
            {tournament:'Cincinnati Open - Cincinnati, OH', category:'WTA 1000', matchType:'WTA', points:2},
            {tournament:'Open - Buenos Aires, ARG', category:'WTA 125', matchType:'WTA', points:5},
            {tournament:'W35 Monastir 3', category:'W35', matchType:'ITF', points:8},
            {tournament:'Wimbledon', category:'GS', matchType:'GS', points:10},
        ];
        context.plannerScheduleScenario = () => ({baseline:200, gain:5, total:205,
            replaced, replacementsByEvent:{mine:replaced}});
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        click('select', 'mine');
        const rendered = root.innerHTML.replace(/<[^>]*>/g, '');
        assert(rendered.includes('WTA 1000 Cincinnati (2 pts)'));
        assert(rendered.includes('WTA 125 Buenos Aires (5 pts)'));
        assert(rendered.includes('W35 Monastir (8 pts)'));
        assert(rendered.includes('Wimbledon (10 pts)'));
        assert(!rendered.includes('Cincinnati Open'));
        assert(rendered.includes('Replaced:'));
        assert(root.innerHTML.includes('<span class="op-week-replaced">'));
        const position=root.innerHTML.indexOf('op-week-replaced');
        assert(root.innerHTML.lastIndexOf('<h3',position) > root.innerHTML.lastIndexOf('</h3>',position));
    """)


def test_qualy_toggle_updates_rounds_and_respects_fixed_entries():
    run_eligibility_check("""
        events.find(event => event.id === 'open').openRoutes = ['MAIN', 'QUAL'];
        context.plannerPointFinishes = (_event, route) => route === 'QUAL'
            ? [{value:'QR1',points:0},{value:'R32',points:2},{value:'QF',points:13}]
            : [{value:'R32',points:1},{value:'QF',points:11}];
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        click('select', 'open');
        assert(context.collectPlannerUrlState().results[0].endsWith('~R32~MAIN'));
        handlers.change({target:{dataset:{finish:'open'},value:'QF'}});
        assert(context.collectPlannerUrlState().results[0].endsWith('~QF~MAIN'));
        click('qualy', 'open');
        assert(context.collectPlannerUrlState().results[0].endsWith('~QF~QUAL'));
        assert(root.innerHTML.includes('value="QR1"'));
        assert(root.innerHTML.includes('QF · 13 pts'));
        handlers.change({target:{dataset:{finish:'open'},value:'QR1'}});
        click('qualy', 'open');
        assert(context.collectPlannerUrlState().results[0].endsWith('~R32~MAIN'));
        assert(!root.innerHTML.includes('value="QR1"'));
        click('clear-schedule', '');
        click('select', 'mine');
        click('qualy', 'mine');
        assert(context.collectPlannerUrlState().results[0].endsWith('~MAIN'));
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
        assert(context.collectPlannerUrlState().results[0].endsWith('~R32~MAIN'));
        assert(!root.innerHTML.includes('value="QR1"'));
        click('qualy', 'alternate');
        assert(context.collectPlannerUrlState().results[0].endsWith('~R32~QUAL'));
        assert(root.innerHTML.includes('value="QR1"'));
        click('qualy', 'alternate');
        assert(context.collectPlannerUrlState().results[0].endsWith('~R32~MAIN'));
        assert(!root.innerHTML.includes('value="QR1"'));
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
            schedule:'alternate',results:'alternate~QF~MAIN'}));
        assert(context.collectPlannerUrlState().results[0].endsWith('~QF~MAIN'));
        click('qualy', 'alternate');
        assert(context.collectPlannerUrlState().results[0].endsWith('~QF~QUAL'));
    """)


@pytest.mark.parametrize("round_code", ["R32", "R64", "R128"])
def test_new_selection_starts_at_the_first_main_draw_round(round_code):
    run_eligibility_check("""
        const firstRound=""" + json.dumps(round_code) + """;
        context.plannerPointFinishes=()=>[
            {value:'QR1',points:0},{value:firstRound,points:1},{value:'QF',points:11}];
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        click('select','open');
        assert(context.collectPlannerUrlState().results[0].endsWith('~'+firstRound+'~MAIN'));
        click('qualy','open');
        assert(context.collectPlannerUrlState().results[0].endsWith('~'+firstRound+'~QUAL'));
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
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
            starting:'2026-11-02', ending:'2026-11-09', schedule:'mine,open,alternate'}));
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['mine', 'alternate']);
        click('select', 'mine');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['alternate']);
        assert(root.innerHTML.includes('data-event="open"'));
        click('select', 'open');
        assert.deepEqual(Array.from(context.collectPlannerUrlState().schedule), ['alternate', 'open']);
        click('clear-schedule', '');
        assert.equal(context.collectPlannerUrlState().schedule, undefined);
    """)


def test_schedule_detects_overlapping_and_same_day_events():
    run_eligibility_check("""
        const first = {...common, name: 'First', startDate: '2026-11-02', endDate: '2026-11-08'};
        const overlaps = {...common, name: 'Overlap', startDate: '2026-11-08', endDate: '2026-11-15'};
        const following = {...common, name: 'Following', startDate: '2026-11-09', endDate: '2026-11-15'};
        assert.equal(context.plannerScheduleConflicts([first, following]).length, 0);
        const conflicts = context.plannerScheduleConflicts([first, overlaps]);
        assert.equal(conflicts.length, 1);
        assert.deepEqual(Array.from(conflicts[0]), ['First', 'Overlap']);
    """)


def test_ending_weeks_merge_shared_slam_deadlines_and_preserve_the_points_cutoff():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.gsDeadlines = [{id:'rg', name:'Roland Garros', year:2027,
            mdCutoff:'2026-11-15', qCutoff:'2026-11-15'}];
        events.find(event => event.id === 'mine').startDate = '2026-11-09';
        events.find(event => event.id === 'alternate').startDate = '2026-11-16';
        let horizon;
        context.plannerScheduleScenario = (_player,_selections,target) => {
            horizon=target; return {baseline:200,gain:0,total:200,replaced:[]};
        };
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player',
            starting:'2026-11-02', ending:'gs-rg-2027-q'}));
        const endingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert(!endingHtml.includes('optgroup'));
        assert.equal((endingHtml.match(/RG&#39;27/g)||[]).length,1);
        assert(endingHtml.includes('value="2026-11-09" selected>RG&#39;27 · 09/11/2026'));
        assert(endingHtml.includes('value="2026-11-16">16/11/2026'));
        assert(endingHtml.includes('value="2027-03-01"'));
        assert(!endingHtml.includes('value="2027-03-08"'));
        assert.equal(horizon,'2026-11-15');
        assert.equal(context.collectPlannerUrlState().ending,'2026-11-09');
        assert.equal(context.plannerSlamOptions()[0].deadlineLabel,"RG'27");
        const startingHtml=root.innerHTML.match(/<select id="op-starting"[^>]*>(.*?)<\/select>/)[1];
        assert(startingHtml.includes('value="2026-11-09">RG&#39;27 · 09/11/2026'));
        assert(root.innerHTML.includes('data-event="mine"'));
        assert(!root.innerHTML.includes('data-event="alternate"'));
        handlers.change({target:{dataset:{filter:'ending'},value:'2026-11-23'}});
        assert(root.innerHTML.includes('data-event="alternate"'));
        assert.equal(horizon,'2026-11-30');
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-11-16'}});
        const laterEndingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert(!laterEndingHtml.includes('RG&#39;27'));
        handlers.change({target:{dataset:{filter:'starting'},value:'2026-10-19'}});
        const fullEndingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert.equal((fullEndingHtml.match(/<option /g)||[]).length,20);
    """)


def test_ao_deadlines_preserve_the_default_ending_week_range_with_distinct_draws():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.gsDeadlines = [{id:'ao',name:'Australian Open',year:2027,
            mdCutoff:'2026-12-07',qCutoff:'2026-12-21'},
            {id:'wim',name:'Wimbledon',year:2027,mdCutoff:'2027-05-17',qCutoff:'2027-05-31'},
            {id:'unknown',name:'Missing dates',year:2027,mdCutoff:'N/A',qCutoff:null}];
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        const endingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert(endingHtml.includes('value="2026-11-30">AO&#39;27 MD · 30/11/2026'));
        assert(endingHtml.includes('value="2026-12-14">AO&#39;27 Q · 14/12/2026'));
        assert(endingHtml.includes('value="2026-12-21">21/12/2026'));
        assert(endingHtml.includes('value="2027-03-01"'));
        assert(!endingHtml.includes('value="2027-03-08"'));
        assert(!endingHtml.includes('WMB'));
        assert(!endingHtml.includes('Missing dates'));
        assert.deepEqual(Array.from(context.plannerSlamOptions(), item => item.deadlineLabel),
            ["AO'27 MD", "AO'27 Q", "WMB'27 MD", "WMB'27 Q"]);
    """)


def test_ending_weeks_extend_through_the_next_qualifying_deadline_with_distinct_draws():
    run_eligibility_check(r"""
        context.WTARG_DATA.planner.gsDeadlines = [{id:'rg',name:'Roland Garros',year:2027,
            mdCutoff:'2027-04-12',qCutoff:'2027-04-26'}];
        await context.restorePlannerUrlState(new URLSearchParams({player:'test-player'}));
        const endingHtml=root.innerHTML.match(/<select id="op-ending"[^>]*>(.*?)<\/select>/)[1];
        assert(endingHtml.includes('value="2027-04-05">RG&#39;27 MD · 05/04/2027'));
        assert(endingHtml.includes('value="2027-04-19">RG&#39;27 Q · 19/04/2027'));
        assert(!endingHtml.includes('value="2027-04-26"'));
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
        context.writeUrlStateForTab('planner', {player: 'Test Player',
            compare: [first], schedule: [first, second], results}, {track: false});
        const params = new URLSearchParams(location.search);
        assert.equal(location.pathname, '/planner/');
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
