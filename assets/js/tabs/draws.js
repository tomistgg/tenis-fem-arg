            // ===== DRAWS =====
            let currentDrawTKey = '';
            let currentDrawType = 'MDS';

            function onDrawTournamentChange(tKey) {
                currentDrawTKey = tKey;
                const info = drawsTournamentInfo[tKey];
                if (!info) return;
                const types = info.types || [];
                if (types.length > 0 && !types.includes(currentDrawType)) {
                    currentDrawType = types[0];
                }
                updateDrawTypeButtons(types);
                loadDraw();
                syncUrlStateForTab('draws');
            }

            function selectDrawType(dtype) {
                currentDrawType = dtype;
                const btns = document.querySelectorAll('.draw-type-btn');
                btns.forEach(b => b.classList.toggle('active', b.dataset.type === dtype));
                loadDraw();
                syncUrlStateForTab('draws');
            }

            function updateDrawTypeButtons(types) {
                const container = document.getElementById('draws-type-btns');
                container.innerHTML = '';
                const labels = {'MDS': 'Main Draw', 'QS': 'Qualifying'};
                types.forEach(t => {
                    const btn = document.createElement('button');
                    btn.className = 'draw-type-btn' + (t === currentDrawType ? ' active' : '');
                    btn.dataset.type = t;
                    btn.textContent = labels[t] || t;
                    btn.onclick = () => selectDrawType(t);
                    container.appendChild(btn);
                });
            }

            function loadDraw() {
                currentDrawFilterRound = 0;
                document.getElementById('draw-filter-reset').classList.remove('visible');
                const key = currentDrawTKey + '|' + currentDrawType;
                const data = drawsData[key];
                const bracket = document.getElementById('draw-bracket');
                if (!data || !data.players || data.players.length === 0) {
                    bracket.innerHTML = '<div class="draw-no-draws">No draw available</div>';
                    return;
                }
                renderBracket(data, bracket);
            }

            function updateDraw() {
                const sel = document.getElementById('draws-tournament-select');
                if (!currentDrawTKey && sel.value) {
                    onDrawTournamentChange(sel.value);
                } else if (currentDrawTKey) {
                    loadDraw();
                }
            }

            function formatDrawName(rawName) {
                if (!rawName) return '';
                let name = rawName.replace(/\.\.\.$/, '').trim();
                // Shorten names > 25 chars: "LASTNAME1 LASTNAME2, First" -> "LASTNAME1 L., First"
                if (name.length > 25) {
                    const ci = name.indexOf(',');
                    if (ci > 0) {
                        const last = name.substring(0, ci).trim();
                        const first = name.substring(ci + 1).trim();
                        const parts = last.split(/\s+/);
                        if (parts.length >= 2) {
                            // Keep first word of last name, abbreviate the rest
                            const shortened = parts[0] + ' ' + parts.slice(1).map(p => p.charAt(0) + '.').join(' ');
                            name = shortened + ', ' + first;
                        }
                    }
                }
                return name;
            }

            function parseScore(scoreStr) {
                if (!scoreStr) return { sets: [], retired: false, walkover: false };
                const parts = scoreStr.trim().split(/\s+/);
                const sets = [];
                let retired = false;
                let walkover = false;
                for (const p of parts) {
                    if (p === 'RET' || p === 'DEF') { retired = true; continue; }
                    if (p === 'W/O' || p === 'WO' || p === 'W.O.') { walkover = true; continue; }
                    // Accept both compact WTA-like set tokens ("64", "76(4)") and match-tiebreak tokens ("11-9").
                    // Also handle legacy compact match-tiebreak encoding like "119" (11-9) or "108" (10-8).
                    const mh = p.match(/^\[?(\d+)[-:\/](\d+)\]?(?:\((\d+)\))?$/);
                    if (mh) {
                        sets.push({ w: parseInt(mh[1], 10), l: parseInt(mh[2], 10), tb: mh[3] || null });
                        continue;
                    }
                    const mc = p.match(/^(\d+)(?:\((\d+)\))?$/);
                    if (mc) {
                        const digits = mc[1];
                        let w = null;
                        let l = null;
                        if (digits.length === 2) {
                            w = parseInt(digits.charAt(0), 10);
                            l = parseInt(digits.charAt(1), 10);
                        } else if (digits.length === 3) {
                            w = parseInt(digits.slice(0, 2), 10);
                            l = parseInt(digits.slice(2), 10);
                        } else if (digits.length === 4) {
                            w = parseInt(digits.slice(0, 2), 10);
                            l = parseInt(digits.slice(2), 10);
                        } else {
                            const mid = Math.floor(digits.length / 2);
                            w = parseInt(digits.slice(0, mid), 10);
                            l = parseInt(digits.slice(mid), 10);
                        }
                        if (!Number.isNaN(w) && !Number.isNaN(l)) {
                            sets.push({ w, l, tb: mc[2] || null });
                        }
                    }
                }
                // Reject live/in-progress scores: every regular set must have one player on 6+ games.
                // Avoids displaying mid-match snapshots like '44 44' as if they were final results.
                if (!retired && !walkover) {
                    for (const s of sets) {
                        if (Math.max(s.w, s.l) < 6) {
                            return { sets: [], retired: false, walkover: false };
                        }
                    }
                }
                return { sets, retired, walkover };
            }

            function isMatchWinner(playerName, winnerName) {
                if (!playerName || !winnerName) return false;
                const truncated = winnerName.trim().endsWith('...');
                const pNorm = playerName.replace(/\.\.\.$/, '').trim().toUpperCase();
                const wNorm = winnerName.replace(/\.\.\.$/, '').trim().toUpperCase();
                if (pNorm === wNorm) return true;
                // playerName is "LASTNAME, First" format; winnerName is "F. Lastname" format
                const commaIdx = pNorm.indexOf(',');
                if (commaIdx > 0) {
                    const playerLast = pNorm.substring(0, commaIdx).trim();
                    const wm = wNorm.match(/^(?:[A-Z]+\.\s+)+(.+)$/);
                    if (wm) {
                        const winnerLast = wm[1].trim();
                        if (playerLast === winnerLast) return true;
                        // Handle truncated names like "Jimenez Kasints..." vs "JIMENEZ KASINTSEVA"
                        if (truncated && winnerLast.length >= 5 && playerLast.startsWith(winnerLast)) return true;
                    }
                }
                return false;
            }

            function getWinnerPlayer(match, players) {
                if (!match || !match.winner_name) return null;
                for (const p of players) {
                    if (isMatchWinner(p.name, match.winner_name)) return p;
                }
                return null;
            }

            function renderPlayer(player, isBye, isQualifier, isWinner, isTop, scoreData, matchConcluded, showWalkover) {
                const flag = player ? countryFlag(player.country, false) : '';
                const flagHtml = '<span class="country">' + flag + '</span>';
                let seedEntry = '<span class="seed-entry"></span>';
                if (player) {
                    let seText = '';
                    if (player.seed && player.entry) {
                        seText = '<span class="seed">' + player.seed + '/' + '</span><span class="entry">' + player.entry + '</span>';
                    } else if (player.seed) {
                        seText = '<span class="seed">' + player.seed + '</span>';
                    } else if (player.entry) {
                        seText = '<span class="entry">' + player.entry + '</span>';
                    }
                    seedEntry = '<span class="seed-entry">' + seText + '</span>';
                }
                let name = '';
                if (player) name = formatDrawName(player.name);
                else if (isBye) name = 'BYE';
                else if (isQualifier) name = 'Qualifier';
                const nameHtml = '<span class="name">' + name + '</span>';
                let setsHtml = '';
                if (scoreData && scoreData.sets && scoreData.sets.length > 0) {
                    const ss = scoreData.sets;
                    for (let i = 0; i < ss.length; i++) {
                        const s = ss[i];
                        const myScore = isWinner ? s.w : s.l;
                        const otherScore = isWinner ? s.l : s.w;
                        const won = myScore > otherScore;
                        const cls = won ? 'won' : 'lost';
                        const tb = (s.tb && !won) ? '<sup>' + s.tb + '</sup>' : '';
                        setsHtml += '<span class="set-score ' + cls + '">' + myScore + tb + '</span>';
                    }
                    if (scoreData.retired) {
                        if (!isWinner) {
                            setsHtml += '<span class="set-score lost">R</span>';
                        } else {
                            setsHtml += '<span class="set-score">&nbsp;</span>';
                        }
                    }
                } else if (matchConcluded && isWinner && showWalkover) {
                    setsHtml += '<span class="set-score won wo">W.O.</span>';
                }
                const cls = 'draw-player' + (isWinner ? ' winner' : '');
                return '<div class="' + cls + '">' + flagHtml + seedEntry + nameHtml + (setsHtml ? '<span class="sets">' + setsHtml + '</span>' : '') + '</div>';
            }

            function renderMatch(p1, p2, isBye1, isBye2, isQ1, isQ2, match, players) {
                const scoreText = (match && match.score) ? String(match.score).trim() : '';
                // Only treat a match as concluded if we have a non-empty score.
                // WTA PDFs often show "advanced" names in later rounds before matches are played (e.g., seeds with byes),
                // and parsing those as winners breaks early-round pairings (Miami WTA 1000 case).
                const matchConcluded = !!(match && match.winner_name && scoreText);
                const scoreData = matchConcluded ? parseScore(scoreText) : null;
                const winnerPlayer = matchConcluded ? getWinnerPlayer(match, players) : null;
                const showWalkover = !!(matchConcluded && scoreData && scoreData.walkover);
                const p1IsWinner = winnerPlayer && p1 && isMatchWinner(p1.name, match.winner_name);
                const p2IsWinner = winnerPlayer && p2 && isMatchWinner(p2.name, match.winner_name);
                return '<div class="draw-match">' +
                    renderPlayer(p1, isBye1, isQ1, p1IsWinner, true, matchConcluded ? scoreData : null, matchConcluded, showWalkover) +
                    renderPlayer(p2, isBye2, isQ2, p2IsWinner, false, matchConcluded ? scoreData : null, matchConcluded, showWalkover) +
                    '</div>';
            }

            function renderBracket(data, container) {
                const players = data.players || [];
                const matches = data.matches || [];
                const byes = new Set(data.byes || []);
                const drawSize = data.draw_size || players.length;
                const pdfRoundLabels = data.round_labels || [];
                const numRounds = data.num_rounds || Math.ceil(Math.log2(drawSize));
                const playersByPos = new Map(players.map(p => [p.pos, p]));
                const playerPosSet = new Set(players.map(p => p.pos));
                const matchMap = new Map(matches.map(m => [`${m.round}:${m.match_num}`, m]));
                const isQualifying = (data.draw_type || '').toUpperCase().includes('QUAL') || currentDrawType === 'QS';

                function getMatch(roundNum, matchNum) {
                    return matchMap.get(`${roundNum}:${matchNum}`) || null;
                }

                function formatRoundLabel(label, roundIdx) {
                    const norm = (label || '').trim();
                    if (isQualifying) {
                        if (/^Round of\s+\d+$/i.test(norm)) {
                            const ordinals = ['1st Round', '2nd Round', '3rd Round', '4th Round', '5th Round', '6th Round'];
                            return ordinals[roundIdx] || ('Round ' + (roundIdx + 1));
                        }
                        return label;
                    }
                    if (/^(1st|2nd|3rd|4th)\s+Round$/i.test(norm) || /^R\d+$/i.test(norm)) {
                        const roundOf = Math.round(drawSize / Math.pow(2, roundIdx));
                        if (roundOf >= 2) return 'Round of ' + roundOf;
                    }
                    return label;
                }

                function getAdvancer(roundNum, matchNum) {
                    if (roundNum <= 0) return null;
                    const match = getMatch(roundNum, matchNum);
                    const scoreText = (match && match.score) ? String(match.score).trim() : '';
                    if (match && match.winner_name && scoreText) {
                        const winner = getWinnerPlayer(match, players);
                        if (winner) return winner;
                        return null;
                    }
                    if (roundNum === 1) {
                        const pos1 = matchNum * 2 + 1;
                        const pos2 = matchNum * 2 + 2;
                        const p1 = playersByPos.get(pos1) || null;
                        const p2 = playersByPos.get(pos2) || null;
                        const bye1 = byes.has(pos1);
                        const bye2 = byes.has(pos2);
                        if (bye1 && !bye2) return p2;
                        if (bye2 && !bye1) return p1;
                        return null;
                    }
                    return null;
                }

                function hasPlayerInRange(startPos, endPos) {
                    for (let pos = startPos; pos <= endPos; pos++) {
                        if (playerPosSet.has(pos)) return true;
                    }
                    return false;
                }

                let html = '';
                for (let r = 0; r < numRounds; r++) {
                    const rawLabel = r < pdfRoundLabels.length ? pdfRoundLabels[r] : 'R' + (r + 1);
                    const label = formatRoundLabel(rawLabel, r);
                    html += '<div class="draw-round" data-round="' + r + '"><div class="draw-round-header" role="button" tabindex="0" data-round="' + r + '" title="Show from this round">' + label + '</div>';

                    if (r === 0) {
                        const numMatches = Math.floor(drawSize / 2);
                        const treatEmptyFirstRoundQualAsBye = isQualifying;
                        for (let m = 0; m < numMatches; m++) {
                            const pos1 = m * 2 + 1;
                            const pos2 = m * 2 + 2;
                            const p1 = playersByPos.get(pos1) || null;
                            const p2 = playersByPos.get(pos2) || null;
                            const isBye1 = byes.has(pos1) || (treatEmptyFirstRoundQualAsBye && !p1);
                            const isBye2 = byes.has(pos2) || (treatEmptyFirstRoundQualAsBye && !p2);
                            const isQ1 = !p1 && !isBye1;
                            const isQ2 = !p2 && !isBye2;
                            const match = getMatch(1, m);
                            html += '<div class="draw-match-wrapper">' + renderMatch(p1, p2, isBye1, isBye2, isQ1, isQ2, match, players) + '</div>';
                        }
                    } else {
                        const numMatches = Math.floor(drawSize / Math.pow(2, r + 1));
                        for (let m = 0; m < numMatches; m++) {
                            const match = getMatch(r + 1, m);
                            const p1 = getAdvancer(r, m * 2);
                            const p2 = getAdvancer(r, m * 2 + 1);
                            const groupStart = m * Math.pow(2, r + 1) + 1;
                            const halfSize = Math.pow(2, r);
                            const topStart = groupStart;
                            const topEnd = groupStart + halfSize - 1;
                            const botStart = groupStart + halfSize;
                            const botEnd = groupStart + Math.pow(2, r + 1) - 1;
                            const topHasPlayer = hasPlayerInRange(topStart, topEnd);
                            const botHasPlayer = hasPlayerInRange(botStart, botEnd);
                            const isBye1 = !p1 && !!p2 && !topHasPlayer;
                            const isBye2 = !p2 && !!p1 && !botHasPlayer;
                            html += '<div class="draw-match-wrapper">' + renderMatch(p1, p2, isBye1, isBye2, false, false, match, players) + '</div>';
                        }
                    }
                    html += '</div>';
                }

                container.innerHTML = html;
                drawConnectors(container);
            }

            function getOffsetRelativeTo(el, ancestor) {
                let x = 0, y = 0;
                let current = el;
                while (current && current !== ancestor) {
                    x += current.offsetLeft;
                    y += current.offsetTop;
                    current = current.offsetParent;
                }
                return { x, y, w: el.offsetWidth, h: el.offsetHeight };
            }

            function drawConnectors(container) {
                const rounds = container.querySelectorAll('.draw-round');
                const oldSvg = container.querySelector('svg');
                if (oldSvg) oldSvg.remove();

                const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;';
                svg.setAttribute('width', container.scrollWidth);
                svg.setAttribute('height', container.scrollHeight);
                container.appendChild(svg);

                for (let r = 0; r < rounds.length - 1; r++) {
                    // Skip connectors from/to hidden rounds
                    if (rounds[r].classList.contains('hidden-round') || rounds[r + 1].classList.contains('hidden-round')) continue;
                    const currMatches = rounds[r].querySelectorAll('.draw-match-wrapper');
                    const nextMatches = rounds[r + 1].querySelectorAll('.draw-match-wrapper');

                    for (let m = 0; m < nextMatches.length; m++) {
                        const topIdx = m * 2;
                        const botIdx = m * 2 + 1;
                        if (topIdx >= currMatches.length) continue;

                        const topMatch = currMatches[topIdx];
                        const botMatch = botIdx < currMatches.length ? currMatches[botIdx] : null;
                        const nextMatch = nextMatches[m];

                        const topPos = getOffsetRelativeTo(topMatch, container);
                        const nextPos = getOffsetRelativeTo(nextMatch, container);

                        const xStart = topPos.x + topPos.w;
                        const xEnd = nextPos.x;
                        const xMid = (xStart + xEnd) / 2;

                        const yT = topPos.y + topPos.h / 2;
                        const yN = nextPos.y + nextPos.h / 2;

                        const pathT = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                        pathT.setAttribute('d', `M${xStart},${yT} H${xMid} V${yN} H${xEnd}`);
                        pathT.setAttribute('fill', 'none');
                        pathT.setAttribute('stroke', '#cbd5e1');
                        pathT.setAttribute('stroke-width', '1');
                        svg.appendChild(pathT);

                        if (botMatch) {
                            const botPos = getOffsetRelativeTo(botMatch, container);
                            const yB = botPos.y + botPos.h / 2;
                            const pathB = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                            pathB.setAttribute('d', `M${xStart},${yB} H${xMid} V${yN} H${xEnd}`);
                            pathB.setAttribute('fill', 'none');
                            pathB.setAttribute('stroke', '#cbd5e1');
                            pathB.setAttribute('stroke-width', '1');
                            svg.appendChild(pathB);
                        }
                    }
                }
            }

            let currentDrawFilterRound = 0;

            function filterDrawFromRound(r) {
                const container = document.getElementById('draw-bracket');
                const rounds = container.querySelectorAll('.draw-round');
                const resetBtn = document.getElementById('draw-filter-reset');

                if (currentDrawFilterRound === r) {
                    resetDrawFilter();
                    return;
                }

                currentDrawFilterRound = r;

                rounds.forEach((round, idx) => {
                    const header = round.querySelector('.draw-round-header');
                    if (idx < r) {
                        round.classList.add('hidden-round');
                    } else {
                        round.classList.remove('hidden-round');
                    }
                    if (header) {
                        header.classList.toggle('active-filter', idx === r);
                    }
                });

                resetBtn.classList.add('visible');
                // Redraw connectors after layout change
                setTimeout(() => drawConnectors(container), 50);
                syncUrlStateForTab('draws');
            }

            function resetDrawFilter() {
                const container = document.getElementById('draw-bracket');
                const rounds = container.querySelectorAll('.draw-round');
                const resetBtn = document.getElementById('draw-filter-reset');

                currentDrawFilterRound = 0;
                rounds.forEach(round => {
                    round.classList.remove('hidden-round');
                    const header = round.querySelector('.draw-round-header');
                    if (header) header.classList.remove('active-filter');
                });
                resetBtn.classList.remove('visible');
                setTimeout(() => drawConnectors(container), 50);
                syncUrlStateForTab('draws');
            }

            (function bindDrawRoundHeaderControls() {
                const container = document.getElementById('draw-bracket');
                if (!container || container._drawRoundHeaderBound) return;

                function activateHeader(header) {
                    const round = Number.parseInt(header.getAttribute('data-round') || '', 10);
                    if (Number.isInteger(round)) filterDrawFromRound(round);
                }

                container.addEventListener('click', function(event) {
                    const header = event.target.closest('.draw-round-header');
                    if (!header || !container.contains(header)) return;
                    activateHeader(header);
                });

                container.addEventListener('keydown', function(event) {
                    if (event.key !== 'Enter' && event.key !== ' ') return;
                    const header = event.target.closest('.draw-round-header');
                    if (!header || !container.contains(header)) return;
                    event.preventDefault();
                    activateHeader(header);
                });

                container._drawRoundHeaderBound = true;
            })();

            // Constrain draw scroll: prevent scrolling left past initial position (scrollLeft=0)
            (function() {
                const wrapper = document.getElementById('draw-bracket-wrapper');
                if (!wrapper) return;
                wrapper.addEventListener('scroll', function() {
                    if (this.scrollLeft < 0) this.scrollLeft = 0;
                });
                // Touch-based constraint for mobile
                let touchStartX = 0;
                let scrollStartX = 0;
                wrapper.addEventListener('touchstart', function(e) {
                    touchStartX = e.touches[0].clientX;
                    scrollStartX = this.scrollLeft;
                }, { passive: true });
                wrapper.addEventListener('touchmove', function(e) {
                    if (this.scrollLeft < 0) this.scrollLeft = 0;
                    // If at left edge and trying to scroll further left, prevent
                    const dx = e.touches[0].clientX - touchStartX;
                    if (scrollStartX === 0 && dx > 0) {
                        this.scrollLeft = 0;
                    }
                }, { passive: true });
            })();

        
