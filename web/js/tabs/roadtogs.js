            // Road to GS: shared lookups (initialised once, reused by renderRoadToGS + computeBest18)
            const _rtgs_roundOrder = {'QR1':1,'QR2':2,'QR3':3,'QR4':4,'Round Robin':4.5,'1st Round':5,'2nd Round':6,'3rd Round':7,'4th Round':8,'5th Round':9,'Quarter Finals':10,'Quarter-finals':10,'Semi-finals':11,'Final':12};
            const _rtgs_categoryToDesc = {
                'GS':'Grand Slam','WTA 1000':'WTA 1000 (56M, 32Q)','WTA 500':'WTA 500 (30/28M, 24/16Q)',
                'WTA 250':'WTA 250 (32M, 24/16Q)','WTA 125':'WTA 125 (32M, 8Q)',
                '125K':'WTA 125 (32M, 8Q)','125K Series':'WTA 125 (32M, 8Q)',
                'W100':'W100 (32M, 32Q)','W75':'W75 (32M, 32Q)','W50':'W50 (32M, 32Q)',
                'W35':'W35 (32M, 64/48/32/24Q)','W15':'W15 (32M, 64/48/32/24Q)'
            };
            const _rtgs_categoryDrawSize = {'GS':128,'WTA 1000':64,'WTA 500':32,'WTA 250':32,'WTA 125':32,'125K':32,'125K Series':32,'W100':32,'W75':32,'W50':32,'W35':32,'W15':32};
            const _rtgs_mandatory1000Names = ['Indian Wells','Miami','Madrid','Rome','Toronto','Montreal','Cincinnati','Beijing'];
            const _rtgs_optional1000Names  = ['Doha','Dubai','Wuhan'];
            // Drop-date and threshold constants â€” single source of truth for the Road-to-GS logic.
            //   2W:      GS / genuine WTA-1000 two-week events drop after 54 weeks.
            //   DEFAULT: every other tournament drops after 53 weeks.
            //   W15W35_DELAY_DAYS: ITF W15/W35 points go live one Monday AFTER the tournament starts.
            //   GS_THRESHOLD_*:    points needed to qualify (Q) / make main draw (MD) at a Grand Slam.
            const _RTGS_DROP_WEEKS_2W = 54;
            const _RTGS_DROP_WEEKS_DEFAULT = 53;
            const _RTGS_W15W35_DELAY_DAYS = 7;
            const _RTGS_GS_THRESHOLD_Q = window.WTARG_DATA.gsThresholdQ;
            const _RTGS_GS_THRESHOLD_MD = window.WTARG_DATA.gsThresholdMd;
            // ITF women's tier categories â€” TWO flavours, used for different decisions:
            //   ALL:         every ITF women's tier. Used to gate "is this a 2-week WTA event?"
            //                â€” an ITF tournament whose name contains e.g. "Madrid" must NOT be
            //                classified as a 2-week WTA-1000 event.
            //   WITH_POINTS: ITF tiers that have rows in the points-distribution / draw-size
            //                lookup tables. Used to choose ITF vs WTA points lookup. Tiers
            //                W40 / W10 / W80 are deliberately excluded here because no point
            //                table exists for them â€” they fall through to the WTA branch fallback.
            const _RTGS_ITF_CATS_ALL = ['W100','W75','W60','W50','W40','W35','W25','W15','W10','W80'];
            const _RTGS_ITF_CATS_WITH_POINTS = ['W100','W75','W60','W50','W35','W25','W15'];
            let _rtgs_pointsLookup = null, _rtgs_itfDrawLookup = null, _rtgs_wtaDrawLookup = null;

            function _rtgs_initLookups() {
                if (!_rtgs_pointsLookup) {
                    _rtgs_pointsLookup = {};
                    pointsDistribution.forEach(p => { _rtgs_pointsLookup[p.Description] = p; });
                    _rtgs_itfDrawLookup = {};
                    itfDrawSizes.forEach(t => {
                        const key = (t.tournamentName||'') + '|' + (t.date||'');
                        _rtgs_itfDrawLookup[key] = {description:t.description, mainDrawSize:t.mainDrawSize};
                        const wm = (t.tournamentName||'').match(/^(.+?)\s*\(Week \d+\)$/);
                        if (wm) _rtgs_itfDrawLookup[wm[1].trim()+'|'+(t.date||'')] = _rtgs_itfDrawLookup[key];
                    });
                    _rtgs_wtaDrawLookup = {};
                    wtaDrawSizes.forEach(t => {
                        if (!t.description || !t.tournamentId) return;
                        _rtgs_wtaDrawLookup[String(parseInt(t.tournamentId)||t.tournamentId)] = {description:t.description, mainDrawSize:t.mainDrawSize};
                    });
                }

                if (!_rtgs_twoWeekFreezeMondays) {
                    const s = new Set();
                    (Array.isArray(historyData) ? historyData : []).forEach(r => {
                        const tName = r['TOURNAMENT'] || '';
                        const draw = (r['DRAW'] || '').toUpperCase();
                        const cat = (r['CATEGORY'] || '').trim();
                        const mt = (r['MATCH_TYPE'] || '').trim();
                        const isGenuine2Week = mt === 'GS' || cat === 'WTA 1000' || cat === 'Premier Mandatory' || cat === 'Premier 5';
                        if (draw === 'M' && isGenuine2Week && _rtgs_twoWeekNames.some(n => tName.includes(n))) {
                            const ds = r['DATE'] || '';
                            let mon = _rtgs_monday(ds);
                            // For freeze detection only: if a 2-week event's first main-draw match is
                            // on Sunday, treat it as part of the upcoming Monday week.
                            const d0 = new Date(ds);
                            if (mon && d0.getUTCDay() === 0) {
                                const m2 = new Date(mon);
                                m2.setUTCDate(m2.getUTCDate() + 7);
                                mon = m2.toISOString().slice(0, 10);
                            }
                            if (mon) {
                                s.add(mon);
                                const w2 = new Date(mon);
                                w2.setUTCDate(w2.getUTCDate() + 7);
                                s.add(w2.toISOString().slice(0, 10));
                            }
                        }
                    });
                    _rtgs_twoWeekFreezeMondays = s;
                }
            }

            function _rtgs_monday(dateStr) {
                const d = new Date(dateStr), day = d.getUTCDay();
                const m = new Date(d);
                m.setUTCDate(d.getUTCDate() + (day===0 ? -6 : 1-day));
                return m.toISOString().slice(0,10);
            }

            // Single source of truth for drop-date computation. Both renderRoadToGS and
            // computeBest18 used to inline this; an out-of-sync edit on one side caused the
            // 767-vs-797 ACC PTS regression that motivated extracting it.
            //
            // Caller must guarantee t.date is set (truthy YYYY-MM-DD); behaviour on a falsy
            // t.date is undefined (Invalid Date arithmetic).
            function _rtgs_computeDropDate(t) {
                const monday = new Date(t.date + 'T00:00:00Z');
                const isW15W35 = t.category === 'W15' || t.category === 'W35';
                const effectiveMonday = new Date(monday);
                if (isW15W35) effectiveMonday.setUTCDate(monday.getUTCDate() + _RTGS_W15W35_DELAY_DAYS);
                const effectiveDateStr = effectiveMonday.toISOString().slice(0, 10);
                const is2WeekEvent = t.isGS || (!_RTGS_ITF_CATS_ALL.includes(t.category) && _rtgs_twoWeekNames.some(n => t.tournament.includes(n)));
                const isConcurrentFreeze = !is2WeekEvent && _rtgs_twoWeekFreezeMondays.has(effectiveDateStr);
                const dropDate = new Date(effectiveMonday);
                if (is2WeekEvent) {
                    dropDate.setUTCDate(effectiveMonday.getUTCDate() + _RTGS_DROP_WEEKS_2W * 7);
                } else if (isConcurrentFreeze) {
                    // Share week1Mon of the concurrent two-week event so all concurrent
                    // tournaments drop on the same date as that event.
                    const prevMon = new Date(effectiveMonday);
                    prevMon.setUTCDate(effectiveMonday.getUTCDate() - 7);
                    const week1Mon = _rtgs_twoWeekFreezeMondays.has(prevMon.toISOString().slice(0, 10)) ? prevMon : effectiveMonday;
                    dropDate.setTime(week1Mon.getTime());
                    dropDate.setUTCDate(dropDate.getUTCDate() + _RTGS_DROP_WEEKS_2W * 7);
                } else {
                    dropDate.setUTCDate(effectiveMonday.getUTCDate() + _RTGS_DROP_WEEKS_DEFAULT * 7);
                }
                return { effectiveMonday, effectiveDateStr, dropDate, is2WeekEvent, isConcurrentFreeze };
            }

            function _rtgs_keepLatestGrandSlamEditions(entries) {
                if (!Array.isArray(entries) || entries.length < 2) {
                    return Array.isArray(entries) ? entries.slice() : [];
                }

                const latestByKey = new Map();
                entries.forEach(t => {
                    if (!t || !t.isGS) return;
                    const key = String(t.tournament || '').trim().toUpperCase() || String(t.tournamentId || '').trim();
                    if (!key) return;
                    const prev = latestByKey.get(key);
                    const tDate = String(t.date || '');
                    const prevDate = prev ? String(prev.date || '') : '';
                    const tMainMonday = String(t.mainMonday || '');
                    const prevMainMonday = prev ? String(prev.mainMonday || '') : '';
                    if (!prev || tDate > prevDate || (tDate === prevDate && tMainMonday > prevMainMonday)) {
                        latestByKey.set(key, t);
                    }
                });

                if (!latestByKey.size) {
                    return entries.slice();
                }

                const keep = new Set(latestByKey.values());
                return entries.filter(t => !t || !t.isGS || keep.has(t));
            }

            // 2-week tournaments that freeze rankings for 2 consecutive weeks
            const _rtgs_twoWeekNames = ['Australian Open','Roland Garros','Wimbledon','US Open','Indian Wells','Miami','Madrid','Internazionali','Rome'];
            // Main-draw mondays of genuine 2-week tournaments (GS + WTA 1000 only).
            // Computed lazily once match history is loaded.
            let _rtgs_twoWeekFreezeMondays = null;

            function _rtgs_mdKey(round, result, drawSize) {
                if (round==='Final') return result==='W'?'W':'F';
                if (result==='W') {
                    const _n32 ={'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                    const _n64 ={'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                    const _n128={'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                    const _nm=drawSize>=128?_n128:(drawSize>=64?_n64:_n32);
                    const _nr=_nm[round]; if (_nr) return _rtgs_mdKey(_nr,'L',drawSize);
                }
                if (round==='Semi-finals') return 'SF';
                if (round==='Quarter-finals') return 'QF';
                if (drawSize===128) { if (round==='4th Round') return 'R16'; if (round==='3rd Round') return 'R32'; if (round==='2nd Round') return 'R64'; if (round==='1st Round') return 'R128'; }
                else if (drawSize===64) { if (round==='3rd Round') return 'R16'; if (round==='2nd Round') return 'R32'; if (round==='1st Round') return 'R64'; }
                else { if (round==='2nd Round') return 'R16'; if (round==='1st Round') return 'R32'; }
                return null;
            }

            function _rtgs_qKey(round, result, hasMain, pTable) {
                if (hasMain) return 'QLFR';
                if (result === 'W') {
                    // Only the final qualifying round upgrades to QLFR.
                    // Earlier qualifying wins should advance to the next QR bucket.
                    const finalQR = pTable ? (pTable['QR3'] != null ? 'QR3' : (pTable['QR2'] != null ? 'QR2' : 'QR1')) : null;
                    if (round === finalQR) return 'QLFR';
                    const nextQual = {'QR1':'QR2','QR2':'QR3'};
                    return nextQual[round] || null;
                }
                return round;
            }

            function _rtgsEmptyBreakdown() {
                return { countable: [], nonCountable: [], totalPoints: 0 };
            }

            function _rtgsComputeBreakdown(selectedPlayer, windowEndStr) {
                _rtgs_initLookups();
                if (!Array.isArray(historyData)) return _rtgsEmptyBreakdown();
                const windowEnd = new Date(windowEndStr);
                const windowStart = new Date(windowEnd);
                windowStart.setDate(windowStart.getDate() - 385); // 55 weeks: wide enough for W15/W35 +7 effective date shift

                const matches = historyData.filter(row => {
                    const mt = (row['MATCH_TYPE']||'').trim();
                    if (mt==='Fed/BJK Cup') return false;
                    const wn = getDisplayName((row['_winnerName']||'').toString().toUpperCase()).toUpperCase();
                    const ln = getDisplayName((row['_loserName']||'').toString().toUpperCase()).toUpperCase();
                    if (wn!==selectedPlayer && ln!==selectedPlayer) return false;
                    const ds = row['DATE']||''; if (!ds) return false;
                    const md = new Date(ds);
                    return md>=windowStart && md<=windowEnd;
                });
                if (!matches.length) return _rtgsEmptyBreakdown();

                const tMap = new Map();
                matches.forEach(row => {
                    const tName=row['TOURNAMENT']||'', ds=row['DATE']||'';
                    const mt=(row['MATCH_TYPE']||'').trim(), cat=(row['CATEGORY']||'').trim();
                    const isGS=mt==='GS', isUC=tName.toUpperCase().includes('UNITED CUP');
                    const mon=_rtgs_monday(ds), draw=(row['DRAW']||'').toUpperCase();
                    const round=row['ROUND']||'', rOrd=_rtgs_roundOrder[round]||0;
                    const wn=getDisplayName((row['_winnerName']||'').toString().toUpperCase()).toUpperCase();
                    const res=wn===selectedPlayer?'W':'L';
                    const tid=(row['TOURNAMENT_ID']||'').trim();
                    const yr=ds.slice(0,4);
                    // Group by tournamentId+year when available so annual editions stay separate
                    // (e.g. Roland Garros 2025 vs Roland Garros 2026), while still combining
                    // qualifying + main-draw weeks of the same edition.
                    const key=(tid?(tid+'|'+yr+'|'+tName):((isGS||isUC)?(mt+'|'+yr+'|'+tName):(mon+'|'+tName)));
                    if (!tMap.has(key)) tMap.set(key, {date:mon,tournament:tName,tournamentId:tid,category:cat,isGS:isGS,isUnitedCup:isUC,bestMainRound:'',bestMainOrder:0,bestMainResult:'',bestQualRound:'',bestQualOrder:0,bestQualResult:'',qualMonday:'',mainMonday:'',ucWins:0,ucTotal:0,ucHasKnockout:false});
                    const e=tMap.get(key);
                    if (isUC) { e.ucTotal++; if (res==='W') e.ucWins++; if (round!=='Round Robin'&&res==='W') e.ucHasKnockout=true; }
                    if (draw==='Q') {
                        if (rOrd>e.bestQualOrder) {e.bestQualRound=round;e.bestQualOrder=rOrd;e.bestQualResult=res;}
                        if (!e.qualMonday||mon<e.qualMonday) e.qualMonday=mon;
                    } else {
                        if (rOrd>e.bestMainOrder) {e.bestMainRound=round;e.bestMainOrder=rOrd;e.bestMainResult=res;}
                        if (!e.mainMonday||mon<e.mainMonday) e.mainMonday=mon;
                    }
                });

                tMap.forEach(t => {
                    if (t.isGS) {
                        if (t.mainMonday) { t.date=t.mainMonday; }
                        else if (t.qualMonday) { const q=new Date(t.qualMonday); q.setUTCDate(q.getUTCDate()+7); t.date=q.toISOString().slice(0,10); }
                    } else if (t.isUnitedCup&&t.mainMonday) { t.date=t.mainMonday; }
                    else if (t.mainMonday) { t.date=t.mainMonday; } // set to main-draw week for multi-week tournaments
                });

                // Filter: only include tournaments whose points are still live at windowEnd.
                // effectiveDateStr > windowEndStr â†’ points not yet live at cutoff.
                // dropDate > windowEnd â†’ points still on the rolling 12-month ranking.
                const ts=_rtgs_keepLatestGrandSlamEditions(Array.from(tMap.values())).filter(t => {
                    if (!t.date) return false;
                    const { effectiveDateStr, dropDate } = _rtgs_computeDropDate(t);
                    if (effectiveDateStr > windowEndStr) return false;
                    return dropDate > windowEnd;
                });
                if (!ts.length) return _rtgsEmptyBreakdown();

                const wtaCats=['WTA 1000','WTA 500','WTA 250','WTA 125','125K','125K Series'];
                ts.forEach(t => {
                    if (t.isUnitedCup) {
                        const uc=_rtgs_pointsLookup['United Cup']; t.points=0;
                        t.roundDisplay=t.ucWins+'W-'+(t.ucTotal-t.ucWins)+'L';
                        if (uc) { const w=t.ucWins,ko=t.ucHasKnockout;
                            if(w>=5)t.points=uc['5W']; else if(w===4)t.points=uc['4W']; else if(w===3)t.points=uc['3W'];
                            else if(w===2&&ko)t.points=uc['2W_KO']; else if(w===2)t.points=uc['2W_RR'];
                            else if(w===1&&ko)t.points=uc['1W_KO']; else if(w===1)t.points=uc['1W_RR'];
                            else t.points=uc['0W']; }
                    } else {
                        const qual=t.bestQualRound&&t.bestQualResult==='W';
                        const ll=t.bestQualRound&&t.bestQualResult==='L'&&!!t.bestMainRound;
                        let desc,drawSize;
                        if (_RTGS_ITF_CATS_WITH_POINTS.includes(t.category)) {
                            const di=_rtgs_itfDrawLookup[t.tournament+'|'+t.date];
                            if(di){desc=di.description;drawSize=di.mainDrawSize>32?64:32;}
                            else{desc=_rtgs_categoryToDesc[t.category]||'';drawSize=_rtgs_categoryDrawSize[t.category]||32;}
                        } else {
                            const nid=t.tournamentId?String(parseInt(t.tournamentId)||t.tournamentId):'';
                            const wi=(wtaCats.includes(t.category)&&nid)?_rtgs_wtaDrawLookup[nid]:null;
                            if(wi){desc=wi.description;drawSize=wi.mainDrawSize>64?128:wi.mainDrawSize>32?64:32;}
                            else{desc=_rtgs_categoryToDesc[t.category]||'';drawSize=_rtgs_categoryDrawSize[t.category]||32;}
                        }
                        const pt=_rtgs_pointsLookup[desc]; t.points=0;
                        if (pt) {
                            if (t.bestMainRound) {
                                const qfl=qual&&t.bestMainRound==='1st Round'&&t.bestMainResult==='L';
                                const lfl=ll&&t.bestMainRound==='1st Round'&&t.bestMainResult==='L';
                                if (!qfl&&!lfl) { const k=_rtgs_mdKey(t.bestMainRound,t.bestMainResult,drawSize); if(k&&pt[k]!=null)t.points+=pt[k]; }
                            }
                            if (t.bestQualRound) {
                                if (ll) { if(pt[t.bestQualRound]!=null)t.points+=pt[t.bestQualRound]; }
                                else { const k=_rtgs_qKey(t.bestQualRound,t.bestQualResult,!!t.bestMainRound,pt); if(k&&pt[k]!=null)t.points+=pt[k]; }
                            }
                        }

                        const finalQualRound=pt?(pt['QR3']!=null?'QR3':(pt['QR2']!=null?'QR2':'QR1')):null;
                        const wonFinalQualRound=!!t.bestQualRound&&t.bestQualResult==='W'&&!t.bestMainRound&&t.bestQualRound===finalQualRound;
                        const qualified=(!!t.bestQualRound&&!!t.bestMainRound&&t.bestQualResult!=='L')||wonFinalQualRound;
                        const nextQualRound={'QR1':'QR2','QR2':'QR3'};
                        const advancedQual=!!t.bestQualRound&&t.bestQualResult==='W'&&!t.bestMainRound&&!wonFinalQualRound;
                        const qualDisplay=qualified?'QLFR':(advancedQual?(nextQualRound[t.bestQualRound]||t.bestQualRound):t.bestQualRound);
                        let mainDisplay=t.bestMainRound;
                        if(t.bestMainRound==='Final'&&t.bestMainResult==='W') mainDisplay='WINNER';
                        t.roundDisplay=t.bestMainRound&&t.bestQualRound
                            ? abbrevRound(mainDisplay)+' + '+qualDisplay
                            : abbrevRound(mainDisplay||qualDisplay||'');
                        if(t.bestMainResult==='W'&&t.bestMainRound&&t.bestMainRound!=='Final') {
                            const next32={'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                            const next64={'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                            const next128={'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                            const nextMap=drawSize>=128?next128:(drawSize>=64?next64:next32);
                            const nextRound=nextMap[t.bestMainRound];
                            if(nextRound) t.roundDisplay=t.bestQualRound?abbrevRound(nextRound)+' + '+qualDisplay:abbrevRound(nextRound);
                        }
                    }
                    const { dropDate }=_rtgs_computeDropDate(t);
                    t.dropDate=dropDate.toISOString().slice(0,10);
                });

                const mGS=[],m1000=[],opt=[],rest=[];
                ts.forEach(t => {
                    const hasMD=!!t.bestMainRound, up=t.tournament.toUpperCase();
                    if(t.isGS&&hasMD) mGS.push(t);
                    else if(t.category==='WTA 1000'&&hasMD&&_rtgs_mandatory1000Names.some(n=>up.includes(n.toUpperCase()))) m1000.push(t);
                    else if(t.category==='WTA 1000'&&hasMD&&_rtgs_optional1000Names.some(n=>up.includes(n.toUpperCase()))) opt.push(t);
                    else rest.push(t);
                });
                m1000.sort((a,b)=>b.points-a.points); opt.sort((a,b)=>b.points-a.points); rest.sort((a,b)=>b.points-a.points);
                const c1000=m1000.slice(0,6), cOpt=opt.slice(0,1);
                mGS.sort((a,b)=>b.points-a.points);
                const mandatory1000=[...c1000,...cOpt].sort((a,b)=>b.points-a.points);
                const mandatory=[...mGS,...mandatory1000];
                const fillPool=[...m1000.slice(6),...opt.slice(1),...rest];
                fillPool.sort((a,b)=>b.points-a.points);
                const filledCountable=fillPool.slice(0,Math.max(0,18-mandatory.length));
                const countable=[...mandatory,...filledCountable];
                const nonCountable=fillPool.slice(filledCountable.length);
                return {
                    countable,
                    nonCountable,
                    totalPoints: countable.reduce((sum,t)=>sum+t.points,0)
                };
            }

            function computeBest18(selectedPlayer, windowEndStr) {
                return _rtgsComputeBreakdown(selectedPlayer, windowEndStr).totalPoints;
            }

            function updateGSCutoffTables(selectedPlayer) {
                gsCutoffs.forEach(gs => {
                    ['q','md'].forEach(type => {
                        const cutoff = type==='q' ? gs.qCutoff : gs.mdCutoff;
                        const accEl = document.getElementById('gs-acc-'+type+'-'+gs.id);
                        const estEl = document.getElementById('gs-est-'+type+'-'+gs.id);
                        if (!accEl||!estEl) return;
                        if (!selectedPlayer||cutoff==='N/A') { accEl.textContent='-'; estEl.textContent='-'; estEl.style.color=''; estEl.style.fontWeight=''; return; }
                        const pts = computeBest18(selectedPlayer, cutoff);
                        accEl.textContent = pts;
                        const est = pts - (type==='q' ? _RTGS_GS_THRESHOLD_Q : _RTGS_GS_THRESHOLD_MD);
                        estEl.textContent = est;
                        estEl.style.fontWeight = 'bold';
                        estEl.style.color = est > 0 ? '#1a7a1a' : est >= -10 ? '#b8860b' : est >= -25 ? '#cc5500' : '#cc0000';
                    });
                });
            }

            // Road to GS
            function abbrevRound(r) {
                return r
                    .replace('WINNER', 'W')
                    .replace('Final', 'F')
                    .replace('Semi-finals', 'SF')
                    .replace('Quarter-finals', 'QF')
                    .replace('4th Round', '4th')
                    .replace('3rd Round', '3rd')
                    .replace('2nd Round', '2nd')
                    .replace('1st Round', '1st');
            }

            function _rtgsSelectedCutoff() {
                const select = document.getElementById('roadtogs-cutoff-select');
                const value = select ? select.value : 'live';
                if (!value || value === 'live') return null;
                const separator = value.lastIndexOf('-');
                if (separator < 1) return null;
                const gsId = value.slice(0, separator);
                const drawType = value.slice(separator + 1);
                const gs = gsCutoffs.find(item => item.id.toLowerCase() === gsId.toLowerCase());
                if (!gs || (drawType !== 'md' && drawType !== 'q')) return null;
                const cutoff = drawType === 'md' ? gs.mdCutoff : gs.qCutoff;
                if (!cutoff || cutoff === 'N/A') return null;
                return { cutoff, drawType, gs };
            }

            function _rtgsDropClass(dropDateStr) {
                if (!dropDateStr) return '';
                const today = new Date();
                today.setUTCHours(0,0,0,0);
                const in14 = new Date(today);
                in14.setUTCDate(today.getUTCDate() + 14);
                const in28 = new Date(today);
                in28.setUTCDate(today.getUTCDate() + 28);
                const dropDate = new Date(dropDateStr);
                if (dropDate <= in14) return ' class="rtgs-warn-14d"';
                if (dropDate <= in28) return ' class="rtgs-warn-28d"';
                return '';
            }

            function _rtgsCategoryKey(t) {
                if (t.isGS || t.category === 'GS') return 'GS';
                if (t.category === 'WTA 1000') return 'WTA 1000';
                if (t.category === 'WTA 500') return 'WTA 500';
                if (t.category === 'WTA 250') return 'WTA 250';
                if (t.category === 'WTA 125' || t.category === '125K' || t.category === '125K Series') return 'WTA 125';
                if (_RTGS_ITF_CATS_ALL.includes(t.category)) return 'ITF';
                return 'OTHER';
            }

            function _rtgsCategoryLabel(key) {
                if (key === 'GS') return 'Grand Slams';
                if (key === 'WTA 1000') return 'WTA 1000';
                if (key === 'WTA 500') return 'WTA 500';
                if (key === 'WTA 250') return 'WTA 250';
                if (key === 'WTA 125') return 'WTA 125';
                if (key === 'ITF') return 'ITF';
                return 'Other';
            }

            function _rtgsIsLocked(t) {
                const hasMainDraw = !!t.bestMainRound;
                return (t.isGS && hasMainDraw) || (t.category === 'WTA 1000' && hasMainDraw);
            }

            function _rtgsTournamentLabel(t) {
                const name = _formatTournName(t.tournament, t.category);
                if (!name) return '';
                if (!_rtgsIsLocked(t)) return name;
                return `${name} <span class="rtgs-lock" title="Locked tournament" aria-label="Locked tournament">&#128274;&#65038;</span>`;
            }

            function _rtgsAppendCategories(parts, list) {
                const order = ['GS', 'WTA 1000', 'WTA 500', 'WTA 250', 'WTA 125', 'ITF', 'OTHER'];
                const groups = new Map();
                list.forEach(t => {
                    const key = _rtgsCategoryKey(t);
                    if (!groups.has(key)) groups.set(key, []);
                    groups.get(key).push(t);
                });
                order.forEach(key => {
                    const rows = groups.get(key) || [];
                    if (!rows.length) return;
                    parts.push(`<tr class="roadtogs-category-separator"><td colspan="5">${_rtgsCategoryLabel(key)}</td></tr>`);
                    rows.forEach(t => {
                        parts.push(`<tr><td>${t.date}</td><td>${_rtgsTournamentLabel(t)}</td><td>${t.roundDisplay}</td><td>${t.points}</td><td${_rtgsDropClass(t.dropDate)}>${t.dropDate}</td></tr>`);
                    });
                });
            }

            function _rtgsRenderBreakdown(tbody, breakdown, includeNonCountable) {
                if (!breakdown.countable.length) {
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">No countable tournaments found for this cutoff.</td></tr>';
                    return;
                }
                const parts = [];
                _rtgsAppendCategories(parts, breakdown.countable);
                if (includeNonCountable && breakdown.nonCountable.length) {
                    parts.push('<tr class="roadtogs-separator"><td colspan="5">NON-COUNTABLE TOURNAMENTS</td></tr>');
                    breakdown.nonCountable.forEach(t => {
                        parts.push(`<tr><td>${t.date}</td><td>${_rtgsTournamentLabel(t)}</td><td>${t.roundDisplay}</td><td>${t.points}</td><td${_rtgsDropClass(t.dropDate)}>${t.dropDate}</td></tr>`);
                    });
                }
                tbody.innerHTML = parts.join('');
            }

            function initRoadToGS() {
                const select = document.getElementById('roadtogsPlayerSelect');
                if (!select) return;
                if (select.dataset.rtgsInit === '1') return;
                select.dataset.rtgsInit = '1';
                $(select).select2({ placeholder: 'Select Player...', allowClear: true, width: '100%' });
                $(select).on('change', renderRoadToGS);
                const cutoffSelect = document.getElementById('roadtogs-cutoff-select');
                if (cutoffSelect) {
                    $(cutoffSelect).select2({ minimumResultsForSearch: Infinity, width: '100%' });
                    $(cutoffSelect).on('change', renderRoadToGS);
                }
                const infoButton = document.querySelector('#view-roadtogs .roadtogs-info-summary');
                const infoPanel = document.getElementById('roadtogs-info-panel');
                if (infoButton && infoPanel) {
                    infoButton.addEventListener('click', () => {
                        const expanded = infoButton.getAttribute('aria-expanded') === 'true';
                        infoButton.setAttribute('aria-expanded', String(!expanded));
                        infoPanel.hidden = expanded;
                    });
                }
            }

            async function renderRoadToGS() {
                const selectedPlayer = getNormalizedPlayerSelection('roadtogsPlayerSelect');
                const tbody = document.getElementById('roadtogs-body');

                if (!selectedPlayer) {
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">Select a player to view their results</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    updateGSCutoffTables('');
                    syncUrlStateForTab('roadtogs');
                    return;
                }

                tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">Loading match history...</td></tr>';
                try {
                    await ensureHistoryDataLoaded();
                } catch (err) {
                    console.error('Failed to load match history:', err);
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-error">Failed to load match history. Please refresh and try again.</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    updateGSCutoffTables('');
                    syncUrlStateForTab('roadtogs');
                    return;
                }
                _rtgs_initLookups();

                const selectedCutoff = _rtgsSelectedCutoff();
                if (selectedCutoff) {
                    const breakdown = _rtgsComputeBreakdown(selectedPlayer, selectedCutoff.cutoff);
                    document.getElementById('roadtogs-points-total').textContent = 'Points: ' + breakdown.totalPoints;
                    updateGSCutoffTables(selectedPlayer);
                    _rtgsRenderBreakdown(tbody, breakdown, true);
                    syncUrlStateForTab('roadtogs');
                    return;
                }

                // Get current date and a wide prefilter window start.
                // We keep this wider than 52 weeks so W15/W35 tournaments (effective +7d)
                // can still be evaluated by the exact week-based cutoff and drop-date logic below.
                const now = new Date();
                const prefilterStart = new Date(now);
                prefilterStart.setDate(prefilterStart.getDate() - 385); // 55 weeks

                // Category to points distribution description mapping (use lower M draw size)
                const categoryToDesc = {
                    'GS': 'Grand Slam',
                    'WTA 1000': 'WTA 1000 (56M, 32Q)',
                    'WTA 500': 'WTA 500 (30/28M, 24/16Q)',
                    'WTA 250': 'WTA 250 (32M, 24/16Q)',
                    'WTA 125': 'WTA 125 (32M, 8Q)',
                    '125K': 'WTA 125 (32M, 8Q)',
                    '125K Series': 'WTA 125 (32M, 8Q)',
                    'W100': 'W100 (32M, 32Q)',
                    'W75': 'W75 (32M, 32Q)',
                    'W50': 'W50 (32M, 32Q)',
                    'W35': 'W35 (32M, 64/48/32/24Q)',
                    'W15': 'W15 (32M, 64/48/32/24Q)'
                };

                // Build points lookup: description -> { W, F, SF, ... }
                const pointsLookup = {};
                pointsDistribution.forEach(p => { pointsLookup[p.Description] = p; });

                // Build ITF draw size lookup: "name|date" -> { description, mainDrawSize }
                const itfDrawLookup = {};
                itfDrawSizes.forEach(t => {
                    const key = (t.tournamentName || '') + '|' + (t.date || '');
                    itfDrawLookup[key] = { description: t.description, mainDrawSize: t.mainDrawSize };
                    // For multi-week entries with "(Week N)", also store with base name
                    const weekMatch = (t.tournamentName || '').match(/^(.+?)\s*\(Week \d+\)$/);
                    if (weekMatch) {
                        const baseKey = weekMatch[1].trim() + '|' + (t.date || '');
                        itfDrawLookup[baseKey] = { description: t.description, mainDrawSize: t.mainDrawSize };
                    }
                });

                // Build WTA draw size lookup by tournament ID (strip leading zeros)
                const wtaDrawLookup = {};
                wtaDrawSizes.forEach(t => {
                    if (!t.description || !t.tournamentId) return;
                    const normId = String(parseInt(t.tournamentId) || t.tournamentId);
                    wtaDrawLookup[normId] = { description: t.description, mainDrawSize: t.mainDrawSize };
                });

                // Draw size per category for mapping round names to point keys
                // GS=128, WTA 1000 (56M)=64, everything else=32
                const categoryDrawSize = {
                    'GS': 128, 'WTA 1000': 64,
                    'WTA 500': 32, 'WTA 250': 32, 'WTA 125': 32,
                    '125K': 32, '125K Series': 32,
                    'W100': 32, 'W75': 32, 'W50': 32, 'W35': 32, 'W15': 32
                };

                // Map a main draw round name to a point key based on draw size
                function getMainDrawPointKey(round, result, drawSize) {
                    if (round === 'Final') return result === 'W' ? 'W' : 'F';
                    if (result === 'W') {
                        // Still in tournament - guaranteed next round; use next round's loss points
                        const _nxt32  = {'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                        const _nxt64  = {'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                        const _nxt128 = {'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                        const _nxtMap = drawSize>=128 ? _nxt128 : (drawSize>=64 ? _nxt64 : _nxt32);
                        const _nxt = _nxtMap[round];
                        if (_nxt) return getMainDrawPointKey(_nxt, 'L', drawSize);
                    }
                    if (round === 'Semi-finals') return 'SF';
                    if (round === 'Quarter-finals') return 'QF';
                    // Numbered rounds depend on draw size
                    if (drawSize === 128) {
                        if (round === '4th Round') return 'R16';
                        if (round === '3rd Round') return 'R32';
                        if (round === '2nd Round') return 'R64';
                        if (round === '1st Round') return 'R128';
                    } else if (drawSize === 64) {
                        if (round === '3rd Round') return 'R16';
                        if (round === '2nd Round') return 'R32';
                        if (round === '1st Round') return 'R64';
                    } else {
                        if (round === '2nd Round') return 'R16';
                        if (round === '1st Round') return 'R32';
                    }
                    return null;
                }

                // Map a qualifying round to a point key __MARKER_TEST__
                // pTable is used to determine the final qualifying round for this tournament
                function getQualPointKey(round, result, hasMainDraw, pTable) {
                    if (hasMainDraw) return 'QLFR';
                    if (result === 'W') {
                        // Won this round â€” check if it's the final qualifying round
                        const finalQR = pTable ? (pTable['QR3'] != null ? 'QR3' : (pTable['QR2'] != null ? 'QR2' : 'QR1')) : null;
                        if (round === finalQR) return 'QLFR';
                        // Still in qualifying: advance to next round (minimum guaranteed result)
                        const nextQual = {'QR1':'QR2','QR2':'QR3'};
                        return nextQual[round] || null;
                    }
                    return round; // lost in this round
                }

                // Prefilter matches for selected player, exclude Fed/BJK Cup.
                // Final "what is in the 52-week table" is decided later from tournament effective week.
                const playerMatches = historyData.filter(row => {
                    const matchType = (row['MATCH_TYPE'] || '').trim();
                    if (matchType === 'Fed/BJK Cup') return false;

                    const wName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase()).toUpperCase();
                    const lName = getDisplayName((row['_loserName'] || '').toString().toUpperCase()).toUpperCase();
                    if (wName !== selectedPlayer && lName !== selectedPlayer) return false;

                    const dateStr = row['DATE'] || '';
                    if (!dateStr) return false;
                    const matchDate = new Date(dateStr);
                    return matchDate >= prefilterStart && matchDate <= now;
                });

                // Helper: compute Monday of a date's week
                function getMonday(dateStr) {
                    const d = new Date(dateStr);
                    const day = d.getUTCDay();
                    const diff = (day === 0) ? -6 : 1 - day;
                    const monday = new Date(d);
                    monday.setUTCDate(d.getUTCDate() + diff);
                    return monday.toISOString().slice(0, 10);
                }

                // Group by tournament + week, track best round per draw type (M/Q).
                // Use tournamentId+year when available so different annual editions
                // remain separate while still combining weeks inside the same edition.
                const tournamentMap = new Map();
                playerMatches.forEach(row => {
                    const tName = row['TOURNAMENT'] || '';
                    const dateStr = row['DATE'] || '';
                    const matchType = (row['MATCH_TYPE'] || '').trim();
                    const category = (row['CATEGORY'] || '').trim();
                    const isGS = matchType === 'GS';
                    const isUnitedCup = tName.toUpperCase().includes('UNITED CUP');
                    const mondayStr = getMonday(dateStr);
                    const draw = (row['DRAW'] || '').toUpperCase();
                    const round = row['ROUND'] || '';
                    const rOrder = _rtgs_roundOrder[round] || 0;

                    // Determine if selected player won or lost this match
                    const wName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase()).toUpperCase();
                    const playerResult = (wName === selectedPlayer) ? 'W' : 'L';

                    const tournamentId = (row['TOURNAMENT_ID'] || '').trim();
                    const yr = dateStr.slice(0, 4);
                    const key = tournamentId
                        ? (tournamentId + '|' + yr + '|' + tName)
                        : ((isGS || isUnitedCup) ? (matchType + '|' + yr + '|' + tName) : (mondayStr + '|' + tName));

                    if (!tournamentMap.has(key)) {
                        tournamentMap.set(key, {
                            date: mondayStr,
                            tournament: tName,
                            tournamentId: tournamentId,
                            category: category,
                            isGS: isGS,
                            isUnitedCup: isUnitedCup,
                            bestMainRound: '',
                            bestMainOrder: 0,
                            bestMainResult: '',
                            bestQualRound: '',
                            bestQualOrder: 0,
                            bestQualResult: '',
                            qualMonday: '',
                            mainMonday: '',
                            ucWins: 0,
                            ucTotal: 0,
                            ucHasKnockout: false
                        });
                    }
                    const entry = tournamentMap.get(key);

                    // United Cup: track win counts and knockout participation
                    if (isUnitedCup) {
                        entry.ucTotal++;
                        if (playerResult === 'W') entry.ucWins++;
                        if (round !== 'Round Robin' && playerResult === 'W') entry.ucHasKnockout = true;
                    }

                    if (draw === 'Q') {
                        if (rOrder > entry.bestQualOrder) {
                            entry.bestQualRound = round;
                            entry.bestQualOrder = rOrder;
                            entry.bestQualResult = playerResult;
                        }
                        if (!entry.qualMonday || mondayStr < entry.qualMonday) {
                            entry.qualMonday = mondayStr;
                        }
                    } else {
                        if (rOrder > entry.bestMainOrder) {
                            entry.bestMainRound = round;
                            entry.bestMainOrder = rOrder;
                            entry.bestMainResult = playerResult;
                        }
                        if (!entry.mainMonday || mondayStr < entry.mainMonday) {
                            entry.mainMonday = mondayStr;
                        }
                    }
                });

                // Compute final date for each tournament
                tournamentMap.forEach(t => {
                    if (t.isGS) {
                        if (t.mainMonday) {
                            t.date = t.mainMonday;
                        } else if (t.qualMonday) {
                            const qMon = new Date(t.qualMonday);
                            qMon.setUTCDate(qMon.getUTCDate() + 7);
                            t.date = qMon.toISOString().slice(0, 10);
                        }
                    } else if (t.isUnitedCup && t.mainMonday) {
                        t.date = t.mainMonday;
                    } else if (t.mainMonday) {
                        t.date = t.mainMonday; // set to main-draw week for multi-week tournaments
                    }
                });

                // Remove entries whose tournament monday is in the same week as (or before) 52 weeks ago.
                // Default: live/current-week window (normal weekly updates).
                // Exception: if CURRENT week is week 1 of a 2-week freeze event, shift cutoff by +1 week
                // so we also remove next week's old results (no ranking update next Monday).
                const _cwMon = (() => { const d = new Date(now); const wd = d.getUTCDay(); d.setUTCDate(d.getUTCDate() - (wd===0?6:wd-1)); d.setUTCHours(0,0,0,0); return d; })();
                const _nextUpdateMon = new Date(_cwMon);
                const _cwMonStr = _cwMon.toISOString().slice(0, 10);
                const _w2Mon = new Date(_cwMon);
                _w2Mon.setUTCDate(_w2Mon.getUTCDate() + 7);
                const _w2MonStr = _w2Mon.toISOString().slice(0, 10);
                const _isFreezeWeek1 = _rtgs_twoWeekFreezeMondays.has(_cwMonStr) && _rtgs_twoWeekFreezeMondays.has(_w2MonStr);
                if (_isFreezeWeek1) _nextUpdateMon.setUTCDate(_nextUpdateMon.getUTCDate() + 7);
                const _52wAgoMon = new Date(_nextUpdateMon);
                _52wAgoMon.setUTCDate(_nextUpdateMon.getUTCDate() - 364);
                tournamentMap.forEach((t, key) => {
                    if (!t.date) return;
                    const effMon = new Date(t.date + 'T00:00:00Z');
                    if (t.category === 'W15' || t.category === 'W35') effMon.setUTCDate(effMon.getUTCDate() + _RTGS_W15W35_DELAY_DAYS);
                    if (effMon <= _52wAgoMon) tournamentMap.delete(key);
                });

                const tournaments = _rtgs_keepLatestGrandSlamEditions(Array.from(tournamentMap.values()));

                if (tournaments.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">No tournaments found in the last 52 weeks.</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    syncUrlStateForTab('roadtogs');
                    return;
                }

                // Calculate points and round display for each tournament
                tournaments.forEach(t => {
                    // United Cup: special win-count based points
                    if (t.isUnitedCup) {
                        const ucTable = pointsLookup['United Cup'];
                        t.roundDisplay = t.ucWins + 'W-' + (t.ucTotal - t.ucWins) + 'L';
                        t.points = 0;
                        if (ucTable) {
                            const w = t.ucWins;
                            const ko = t.ucHasKnockout;
                            if (w >= 5) t.points = ucTable['5W'];
                            else if (w === 4) t.points = ucTable['4W'];
                            else if (w === 3) t.points = ucTable['3W'];
                            else if (w === 2 && ko) t.points = ucTable['2W_KO'];
                            else if (w === 2) t.points = ucTable['2W_RR'];
                            else if (w === 1 && ko) t.points = ucTable['1W_KO'];
                            else if (w === 1) t.points = ucTable['1W_RR'];
                            else t.points = ucTable['0W'];
                        }
                    } else {

                    // Determine draw size and points table first (needed to identify final qualifying round)
                    let desc, drawSize;
                    if (_RTGS_ITF_CATS_WITH_POINTS.includes(t.category)) {
                        const dsInfo = itfDrawLookup[t.tournament + '|' + t.date];
                        if (dsInfo) {
                            desc = dsInfo.description;
                            drawSize = dsInfo.mainDrawSize > 32 ? 64 : 32;
                        } else {
                            console.debug(`[Road to GS] ITF draw size fallback: "${t.tournament}" (${t.date}) not found in itfDrawSizes, using default`);
                            desc = categoryToDesc[t.category] || '';
                            drawSize = categoryDrawSize[t.category] || 32;
                        }
                    } else {
                        // For WTA tournaments, look up actual draw size description by tournament ID
                        const wtaCategories = ['WTA 1000','WTA 500','WTA 250','WTA 125','125K','125K Series'];
                        const wtaNormId = t.tournamentId ? String(parseInt(t.tournamentId) || t.tournamentId) : '';
                        const wtaInfo = (wtaCategories.includes(t.category) && wtaNormId) ? wtaDrawLookup[wtaNormId] : null;
                        if (wtaInfo) {
                            desc = wtaInfo.description;
                            drawSize = wtaInfo.mainDrawSize > 64 ? 128 : (wtaInfo.mainDrawSize > 32 ? 64 : 32);
                        } else {
                            if (wtaCategories.includes(t.category)) {
                                console.debug(`[Road to GS] WTA draw size fallback: "${t.tournament}" (${t.date}) not found in wtaDrawSizes, using default`);
                            }
                            desc = categoryToDesc[t.category] || '';
                            drawSize = categoryDrawSize[t.category] || 32;
                        }
                    }
                    const pTable = pointsLookup[desc];

                    // Final qualifying round = highest QR key with non-null points (GS has QR3; WTA/ITF end at QR2 or QR1)
                    const finalQualRound = pTable ? (pTable['QR3'] != null ? 'QR3' : (pTable['QR2'] != null ? 'QR2' : 'QR1')) : null;

                    // Determine qualifier vs lucky loser status
                    // qualified = entered main draw after qualifying, OR won the final qualifying round (before main draw starts)
                    const wonFinalQualRound = !!t.bestQualRound && t.bestQualResult === 'W' && !t.bestMainRound && t.bestQualRound === finalQualRound;
                    const qualified = (!!t.bestQualRound && !!t.bestMainRound && t.bestQualResult !== 'L') || wonFinalQualRound;
                    const isLuckyLoser = t.bestQualRound && t.bestQualResult === 'L' && !!t.bestMainRound;

                    // Qualifying display
                    // Still in qualifying (won last match, not the final round): advance to next QR (minimum guaranteed)
                    const _nextQualRound = {'QR1':'QR2','QR2':'QR3'};
                    const _advancedQual = !!t.bestQualRound && t.bestQualResult === 'W' && !t.bestMainRound && !wonFinalQualRound;
                    const qualDisplay = qualified ? 'QLFR' : (_advancedQual ? (_nextQualRound[t.bestQualRound] || t.bestQualRound) : t.bestQualRound);

                    // Main draw display: "WINNER" if won the final
                    let mainDisplay = t.bestMainRound;
                    if (t.bestMainRound === 'Final' && t.bestMainResult === 'W') {
                        mainDisplay = 'WINNER';
                    }

                    // Build round display
                    if (t.bestMainRound && t.bestQualRound) {
                        t.roundDisplay = abbrevRound(mainDisplay) + ' + ' + qualDisplay;
                    } else {
                        t.roundDisplay = abbrevRound(mainDisplay || qualDisplay || '');
                    }

                    // If player won their last round (still active), advance roundDisplay to guaranteed next round
                    if (t.bestMainResult === 'W' && t.bestMainRound && t.bestMainRound !== 'Final') {
                        const _rd32  = {'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                        const _rd64  = {'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                        const _rd128 = {'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'};
                        const _rdMap = drawSize>=128 ? _rd128 : (drawSize>=64 ? _rd64 : _rd32);
                        const _rdNxt = _rdMap[t.bestMainRound];
                        if (_rdNxt) {
                            const _rdAbbr = abbrevRound(_rdNxt);
                            t.roundDisplay = t.bestQualRound ? (_rdAbbr + ' + ' + qualDisplay) : _rdAbbr;
                        }
                    }
                    t.points = 0;
                    if (pTable) {
                        // Main draw points
                        if (t.bestMainRound) {
                            // Qualifier who lost 1st round: no MD points
                            const qualFirstRoundLoss = qualified && t.bestMainRound === '1st Round' && t.bestMainResult === 'L';
                            // Lucky loser who lost 1st round: no MD points
                            const llFirstRoundLoss = isLuckyLoser && t.bestMainRound === '1st Round' && t.bestMainResult === 'L';
                            if (!qualFirstRoundLoss && !llFirstRoundLoss) {
                                const mdKey = getMainDrawPointKey(t.bestMainRound, t.bestMainResult, drawSize);
                                if (mdKey && pTable[mdKey] != null) t.points += pTable[mdKey];
                            }
                        }
                        // Qualifying points
                        if (t.bestQualRound) {
                            if (isLuckyLoser) {
                                // Lucky loser: points for best qualifying round lost, not QLFR
                                const qKey = t.bestQualRound; // QR1, QR2, QR3
                                if (pTable[qKey] != null) t.points += pTable[qKey];
                            } else {
                                const qKey = getQualPointKey(t.bestQualRound, t.bestQualResult, !!t.bestMainRound, pTable);
                                if (qKey && pTable[qKey] != null) t.points += pTable[qKey];
                            }
                        }
                    }
                    } // end else (non-United Cup)

                    const { dropDate } = _rtgs_computeDropDate(t);
                    t.dropDate = dropDate.toISOString().slice(0, 10);
                });

                // Classify tournaments
                const mandatoryGS = [];
                const mandatory1000 = [];
                const optional1000 = [];
                const rest = [];

                tournaments.forEach(t => {
                    const hasMD = !!t.bestMainRound;
                    const tUpper = t.tournament.toUpperCase();

                    if (t.isGS && hasMD) {
                        t.mandatory = true;
                        mandatoryGS.push(t);
                    } else if (t.category === 'WTA 1000' && hasMD && _rtgs_mandatory1000Names.some(n => tUpper.includes(n.toUpperCase()))) {
                        mandatory1000.push(t);
                    } else if (t.category === 'WTA 1000' && hasMD && _rtgs_optional1000Names.some(n => tUpper.includes(n.toUpperCase()))) {
                        optional1000.push(t);
                    } else {
                        rest.push(t);
                    }
                });

                // Sort each group by points descending
                mandatory1000.sort((a, b) => b.points - a.points);
                optional1000.sort((a, b) => b.points - a.points);
                rest.sort((a, b) => b.points - a.points);

                // Best 6 mandatory WTA 1000
                const counted1000 = mandatory1000.slice(0, 6);
                counted1000.forEach(t => { t.mandatory = true; });
                const uncounted1000 = mandatory1000.slice(6);

                // Best 1 optional WTA 1000
                const countedOpt = optional1000.slice(0, 1);
                countedOpt.forEach(t => { t.mandatory = true; });
                const uncountedOpt = optional1000.slice(1);

                // Combine mandatory countable tournaments
                const mandatoryAll = [...mandatoryGS, ...counted1000, ...countedOpt];
                const mandatoryCount = mandatoryAll.length;

                // Fill remaining spots up to 18 from rest + uncounted WTA 1000s
                const fillPool = [...uncounted1000, ...uncountedOpt, ...rest];
                fillPool.sort((a, b) => b.points - a.points);
                const fillSlots = Math.max(0, 18 - mandatoryCount);
                const filledCountable = fillPool.slice(0, fillSlots);
                const nonCountable = fillPool.slice(fillSlots);

                // Build final ordered list grouped by tier, each sorted by points desc
                mandatoryGS.sort((a, b) => b.points - a.points);
                const allMandatory1000 = [...counted1000, ...countedOpt];
                allMandatory1000.sort((a, b) => b.points - a.points);
                filledCountable.sort((a, b) => b.points - a.points);
                const countable = [...mandatoryGS, ...allMandatory1000, ...filledCountable];
                nonCountable.sort((a, b) => b.points - a.points);

                const totalPoints = countable.reduce((sum, t) => sum + t.points, 0);
                document.getElementById('roadtogs-points-total').textContent = 'Points: ' + totalPoints;
                updateGSCutoffTables(selectedPlayer);

                _rtgsRenderBreakdown(tbody, { countable, nonCountable, totalPoints }, true);
                syncUrlStateForTab('roadtogs');
            }

            document.addEventListener('DOMContentLoaded', initRoadToGS);
