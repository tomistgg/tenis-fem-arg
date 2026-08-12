                    (function() {
                        var tsData = window.WTARG_DATA.tstrength;
                        var tsSort = 'date';
                        var tsView = 'MD'; // 'MD' or 'Q'
                        window.__wtargTStrengthSort = tsSort;
                        window.__wtargTStrengthView = tsView;
                        var levelColors = {"WTA 1000":"#d946ef55","WTA 500":"#aa00ff88","WTA 250":"#0055ff88","WTA 125":"#ffaa0088"};
                        var surfaceColors = {"Hard":"#0055ff88","Clay":"#ff550088","Grass":"#00bb3388","Carpet":"#aa00ff88"};
                        var regionColors = {"Europe":"#0055ff88","North America":"#ff111188","South America":"#00bb3388","Asia":"#ffaa0088","Oceania":"#aa00ff88","Middle East":"#ff660088","Africa":"#ff330088"};

                        function tsGradient(val, minV, maxV) {
                            if (maxV <= minV) return '#f1f5f9';
                            var t = (val - minV) / (maxV - minV);
                            t = Math.max(0, Math.min(1, t));
                            var r, g, b;
                            if (t < 0.5) {
                                var p = t * 2;
                                r = Math.round(0 + p * (255 - 0));
                                g = Math.round(200 + p * (220 - 200));
                                b = Math.round(0 + p * (0 - 0));
                            } else {
                                var p = (t - 0.5) * 2;
                                r = Math.round(255 + p * (220 - 255));
                                g = Math.round(220 + p * (0 - 220));
                                b = Math.round(0 + p * (0 - 0));
                            }
                            return 'rgba(' + r + ',' + g + ',' + b + ',0.50)';
                        }

                        window.tsToggleSort = function() {
                            tsSort = tsSort === 'strength' ? 'date' : 'strength';
                            window.__wtargTStrengthSort = tsSort;
                            document.getElementById('ts-sort-toggle').textContent = tsSort === 'strength' ? 'Order by Date' : 'Order by Strength';
                            tsRender();
                        };

                        function tsUpdateViewToggle() {
                            var btn = document.getElementById('ts-view-toggle');
                            if (!btn) return;
                            btn.textContent = (tsView === 'MD') ? 'View Qualy' : 'View MD';
                        }

                        window.tsToggleView = function() {
                            tsView = (tsView === 'MD') ? 'Q' : 'MD';
                            window.__wtargTStrengthView = tsView;
                            tsUpdateViewToggle();
                            tsRender();
                        };

                        window.__restoreTStrengthState = function(params) {
                            function setSelectFromParam(id, paramName) {
                                var el = document.getElementById(id);
                                if (!el || !params.has(paramName)) return;
                                if (window.setSelectValueFromSlug) {
                                    setSelectValueFromSlug(el, params.get(paramName));
                                    return;
                                }
                                el.value = params.get(paramName) || el.value;
                            }
                            setSelectFromParam('ts-filter-year', 'year');
                            setSelectFromParam('ts-filter-level', 'level');
                            setSelectFromParam('ts-filter-surface', 'surface');
                            setSelectFromParam('ts-filter-region', 'region');
                            var draw = (params.get('draw') || '').toUpperCase();
                            if (draw === 'Q' || draw === 'QUALY') tsView = 'Q';
                            else if (draw === 'MD' || draw === 'M') tsView = 'MD';
                            var sort = (params.get('sort') || '').toLowerCase();
                            if (sort === 'strength' || sort === 'date') tsSort = sort;
                            window.__wtargTStrengthSort = tsSort;
                            window.__wtargTStrengthView = tsView;
                            document.getElementById('ts-sort-toggle').textContent = tsSort === 'strength' ? 'Order by Date' : 'Order by Strength';
                            tsUpdateViewToggle();
                            tsRender();
                        };

                        window.tsRender = function() {
                            var fy = document.getElementById('ts-filter-year').value;
                            var fl = document.getElementById('ts-filter-level').value;
                            var fs = document.getElementById('ts-filter-surface').value;
                            var fr = document.getElementById('ts-filter-region').value;
                            var filtered = tsData.filter(function(t) {
                                if ((t.year || '2025') !== fy) return false;
                                if (fl && t.level !== fl) return false;
                                if (fs && t.surface !== fs) return false;
                                if (fr && t.region !== fr) return false;
                                var d = (t.draw || 'MD');
                                if (tsView === 'Q') return d === 'Q' || d === 'QUALY';
                                return d === 'MD' || d === 'M' || d === 'MAIN';
                            });
                            if (tsSort === 'strength') {
                                filtered.sort(function(a, b) { return a.gm - b.gm; });
                            } else {
                                filtered.sort(function(a, b) { return a.startDate < b.startDate ? -1 : a.startDate > b.startDate ? 1 : 0; });
                            }
                            var gmVals = filtered.map(function(t) { return t.gm; });
                            var hmVals = filtered.map(function(t) { return t.hm; });
                            var gmMin = Math.min.apply(null, gmVals), gmMax = Math.max.apply(null, gmVals);
                            var hmMin = Math.min.apply(null, hmVals), hmMax = Math.max.apply(null, hmVals);
                            var tbody = document.getElementById('tstrength-tbody');
                            var html = '';
                            var isMobile = window.innerWidth <= 768;
                            var regionShort = {"North America":"NA","South America":"SA","Central America":"CA","Caribbean":"Carib","Middle East":"ME","Europe":"EU","Asia":"AS","Oceania":"OC","Africa":"AF"};
                            var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                            function ordinal(d) { var s = ['th','st','nd','rd']; var v = d % 100; return d + (s[(v-20)%10] || s[v] || s[0]); }
                            function fmtDate(ds) {
                                var p = ds.split('-'); var m = parseInt(p[1],10)-1; var d = parseInt(p[2],10);
                                return months[m] + ' ' + ordinal(d);
                            }
                            function cleanName(n) {
                                var cleaned = n.replace(/\s*\d{3}\s*/g, ' ').replace(/\s+/g,' ').trim();
                                var hashMatch = n.match(/#\d+/);
                                if (hashMatch && cleaned.indexOf(hashMatch[0]) === -1) cleaned += ' ' + hashMatch[0];
                                return cleaned;
                            }
                            for (var i = 0; i < filtered.length; i++) {
                                var t = filtered[i];
                                var lc = levelColors[t.level] || '';
                                var sc = surfaceColors[t.surface] || '';
                                var rc = regionColors[t.region] || '';
                                var gmBg = tsGradient(t.gm, gmMin, gmMax);
                                var hmBg = tsGradient(t.hm, hmMin, hmMax);
                                var dateStr = fmtDate(t.startDate);
                                var levelStr = isMobile ? t.level.replace('WTA ','') : t.level;
                                var regionStr = isMobile ? (regionShort[t.region] || t.region || '') : (t.region || '');
                                var nameStr = cleanName(t.name);
                                html += '<tr>';
                                html += '<td class="ts-rank-num">' + (i + 1) + '</td>';
                                html += '<td class="ts-gm" style="background:' + gmBg + '">' + t.gm + '</td>';
                                html += '<td class="ts-hm" style="background:' + hmBg + '">' + t.hm + '</td>';
                                html += '<td>' + dateStr + '</td>';
                                html += '<td class="ts-name">' + nameStr + '</td>';
                                html += '<td style="background:' + lc + '">' + levelStr + '</td>';
                                html += '<td style="background:' + sc + '">' + t.surface + '</td>';
                                html += '<td style="background:' + rc + '">' + regionStr + '</td>';
                                html += '<td>' + t.playerCount + '</td>';
                                html += '</tr>';
                            }
                            tbody.innerHTML = html;
                            window.__wtargTStrengthSort = tsSort;
                            window.__wtargTStrengthView = tsView;
                            if (window.syncUrlStateForTab) window.syncUrlStateForTab('tstrength');
                        };
                        tsUpdateViewToggle();
                        tsRender();
                    })();
                    
