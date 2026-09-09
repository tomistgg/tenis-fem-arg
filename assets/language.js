/* Translate presentation only. Source data, filter values and URL keys stay canonical. */
(function () {
    'use strict';
    const catalog = window.WTARG_TRANSLATIONS || {};
    const originals = new WeakMap();
    const attributes = new WeakMap();
    const templates = {};
    for (const [locale, dictionary] of Object.entries(catalog)) {
        templates[locale] = Object.keys(dictionary).filter(key => key.includes('{')).map(key => {
            const fields = [];
            const pattern = key.split(/(\{\w+\})/).map(part => {
                if (/^\{\w+\}$/.test(part)) { fields.push(part); return '(.+?)'; }
                return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            }).join('');
            return { key, fields, pattern: new RegExp('^' + pattern + '$') };
        });
    }
    let language = 'en';
    try { language = localStorage.getItem('wtarg-language') === 'es' ? 'es' : 'en'; } catch (_) {}

    // These containers hold proper names, never interface copy (even if a name
    // happens to equal an English dictionary key, e.g. a tournament named "Open").
    const protectedSelector = [
        'script', 'style', 'svg', '[translate="no"]', '[data-no-translate]', '.language-toggle',
        '.calendar-tournament', '.calendar-change-item strong', 'td.col-name', '#schedule-table tbody b',
        '.entry-menu-item', '#entry-title', '.entry-player-col:not(th)', '.history-player-name',
        '#rankings-body tr:not(.rankings-system-row) td:nth-child(2)',
        '#history-body tr td:nth-child(2):not([colspan])', '.ts-name', '.draw-player .name:not([data-ui-label])',
        '.draws-tournament-option', '.draws-tournament-name', '.national-opponent-name',
        '.national-opponent-player', '.milestones-achiever', '#milestones-table tbody td:first-child:not([colspan])',
        '#milestones-historical-body td',
        '#milestones-live-body td:nth-child(2)', '#milestones-expired-body td:nth-child(2)',
        '#roadtogs-body td:nth-child(2)', '.bjkc-series-table td:first-child', '.bjkc-series-table td:last-child'
    ].join(',');
    const nameSelect = /player|opponent|tournament/i;
    const placeholders = new Set(['', '__ALL__']);

    function translate(text) {
        if (language === 'en') return text;
        const dictionary = catalog[language] || {};
        const key = text.trim().replace(/\s+/g, ' ');
        let result = dictionary[key];
        if (result === undefined) {
            for (const { key: template, fields, pattern } of templates[language] || []) {
                const match = key.match(pattern);
                if (!match) continue;
                let value = match[1];
                // Only translate the date/month and surface, never a renamed tournament.
                if (template === 'Week of {value}') {
                    value = translate(value);
                } else if (template === 'Surface changed to {value}') {
                    value = dictionary[value] || value;
                }
                match[1] = value;
                result = dictionary[template].replace(/\{\w+\}/g, field => match[fields.indexOf(field) + 1] || field);
                break;
            }
        }
        return result === undefined ? text : text.replace(text.trim(), result);
    }

    function translateWeekHeader(text) {
        const source = text.trim().replace(/^Week of\s+/i, '');
        if (language === 'en') return text.replace(text.trim(), source);
        const match = source.match(/^([A-Za-z]+)\s+(\d{1,2})(,?\s+\d{4})?$/);
        if (!match) return translate(source);
        const monthKey = match[1].charAt(0).toUpperCase() + match[1].slice(1).toLowerCase();
        const month = (catalog[language] || {})[monthKey] || match[1];
        const year = match[3] ? match[3].replace(',', '').trim() : '';
        const rendered = `${match[2]} de ${month.toLocaleLowerCase(language)}${year ? ` de ${year}` : ''}`;
        return text.replace(text.trim(), rendered);
    }

    function translateCutoffDate(text) {
        if (language === 'en') return text;
        const source = text.trim();
        const match = source.match(/^([A-Za-z]{3})\s+(\d{1,2})$/);
        if (!match) return translate(text);
        const candidate = (catalog[language] || {})[match[1]];
        const month = candidate && candidate.length <= 3 ? candidate : match[1];
        return text.replace(source, `${month} ${match[2]}`);
    }

    function protectedText(element) {
        if (element.closest(protectedSelector)) return true;
        const option = element.closest('option');
        if (option && option.parentElement && nameSelect.test(option.parentElement.id)) {
            return !placeholders.has(option.value);
        }
        // Select2 copies option labels into separate elements. Protect name selectors,
        // but continue translating non-name controls such as the points cutoff.
        const selection = element.closest('.select2-selection__rendered');
        const result = element.closest('.select2-results__option:not(.select2-results__message)');
        const owner = selection ? selection.id : result && result.parentElement.id;
        if (owner && nameSelect.test(owner)) {
            const key = element.textContent.trim();
            return !['Select Player...', 'Select a player...', 'All Tournaments', 'All Opponents',
                'ALL PLAYERS', 'Seleccionar jugadora...', 'Seleccionar una jugadora...',
                'Todos los torneos', 'Todas las rivales', 'TODAS LAS JUGADORAS'].includes(key);
        }
        return false;
    }

    function textNode(node) {
        if (!node.parentElement || protectedText(node.parentElement) || !node.data.trim()) return;
        const previous = originals.get(node);
        const source = previous && node.data === previous.rendered ? previous.source : node.data;
        let rendered;
        const keyedElement = node.parentElement.closest('[data-i18n-key]');
        if (keyedElement && language !== 'en') {
            const keyedValue = (catalog[language] || {})[keyedElement.dataset.i18nKey];
            rendered = keyedValue === undefined ? source : source.replace(source.trim(), keyedValue);
        } else if (node.parentElement.closest('.cal-week-header, .entry-menu-week')) rendered = translateWeekHeader(source);
        else if (node.parentElement.closest('.gs-cutoff-date')) rendered = translateCutoffDate(source);
        else rendered = translate(source);
        // An option without an explicit value derives its value from its label.
        const option = node.parentElement.closest('option');
        if (option && !option.hasAttribute('value')) option.setAttribute('value', option.value);
        originals.set(node, { source, rendered });
        if (node.data !== rendered) node.data = rendered;
    }

    function elementAttributes(element) {
        if (protectedText(element)) return;
        const saved = attributes.get(element) || {};
        for (const name of ['title', 'aria-label', 'placeholder', 'alt']) {
            if (!element.hasAttribute(name)) continue;
            const current = element.getAttribute(name);
            const previous = saved[name];
            const source = previous && current === previous.rendered ? previous.source : current;
            const rendered = translate(source);
            saved[name] = { source, rendered };
            if (rendered !== current) element.setAttribute(name, rendered);
        }
        attributes.set(element, saved);
    }

    function apply(root) {
        if (root.nodeType === Node.TEXT_NODE) { textNode(root); return; }
        if (root.nodeType !== Node.ELEMENT_NODE || root.closest(protectedSelector)) return;
        elementAttributes(root);
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                return node.nodeType === Node.ELEMENT_NODE && node.matches(protectedSelector)
                    ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
            }
        });
        let node;
        while ((node = walker.nextNode())) {
            if (node.nodeType === Node.TEXT_NODE) textNode(node);
            else elementAttributes(node);
        }
    }

    function updateButtons() {
        document.querySelectorAll('.language-toggle').forEach(button => {
            const target = language === 'en' ? 'es' : 'en';
            button.querySelector('img').src = 'assets/flags/' + (target === 'es' ? 'es' : 'gb') + '.svg';
            button.querySelector('span').textContent = target.toUpperCase();
            button.setAttribute('aria-label', target === 'es' ? 'Cambiar a español' : 'Switch to English');
            button.title = target === 'es' ? 'Cambiar a español' : 'Switch to English';
        });
    }

    function makeButton() {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'language-toggle';
        button.setAttribute('translate', 'no');
        button.innerHTML = '<img alt="" aria-hidden="true"><span></span>';
        button.addEventListener('click', () => setLanguage(language === 'en' ? 'es' : 'en'));
        return button;
    }

    function mountButtons() {
        document.querySelectorAll('.home-dark-btn, .dark-mode-btn, .mobile-header-theme').forEach(theme => {
            if (theme.dataset.languageMounted) return;
            theme.dataset.languageMounted = '1';
            if (theme.classList.contains('home-dark-btn')) {
                const row = document.createElement('div');
                row.className = 'home-preferences';
                theme.before(row);
                row.append(theme, makeButton());
            } else {
                theme.before(makeButton());
            }
        });
        updateButtons();
    }

    const observer = new MutationObserver(records => {
        observer.disconnect();
        mountButtons();
        for (const record of records) {
            if (record.type === 'childList') record.addedNodes.forEach(apply);
            else if (record.type === 'characterData') textNode(record.target);
            else elementAttributes(record.target);
        }
        observe();
    });
    function observe() {
        observer.observe(document.body, { subtree: true, childList: true, characterData: true,
            attributes: true, attributeFilter: ['title', 'aria-label', 'placeholder', 'alt'] });
    }
    function setLanguage(next) {
        observer.disconnect();
        language = next === 'es' ? 'es' : 'en';
        document.documentElement.lang = language;
        try { localStorage.setItem('wtarg-language', language); } catch (_) {}
        mountButtons();
        apply(document.body);
        observe();
        window.dispatchEvent(new CustomEvent('wtarg-language-change', { detail: { language } }));
    }
    window.WTARG_I18N = { setLanguage, translate, get language() { return language; } };
    document.documentElement.lang = language;
    function init() { setLanguage(language); }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
    else init();
})();
