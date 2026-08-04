/* WTARG responsive application shell.
   Keeps navigation structure and visual hierarchy separate from data rendering. */
(function () {
    'use strict';

    var PRIMARY_TABS = ['entrylists', 'roadtogs', 'calendar', 'upcoming'];
    var SECONDARY_TABS = ['history', 'draws', 'rankings', 'gallery', 'tstrength', 'fedbcup'];
    var TAB_LABELS = {
        home: 'Home',
        entrylists: 'Entry Lists',
        roadtogs: 'Points Breakdown',
        calendar: 'Calendar',
        upcoming: 'Schedule',
        history: 'Match History',
        draws: 'Draws',
        rankings: 'WTA Rankings',
        gallery: 'Photos',
        tstrength: 'WTA Tournament Strength',
        fedbcup: 'Fed / BJK Cup'
    };
    var TAB_ICONS = {
        entrylists: 'assets/files.png',
        roadtogs: 'assets/data.png',
        calendar: 'assets/calendar.png',
        upcoming: 'assets/trophy.png',
        history: 'assets/tennis-player.png',
        draws: 'assets/tournament.png',
        rankings: 'assets/list.png',
        gallery: 'assets/camera.png',
        tstrength: 'assets/score-board.png',
        fedbcup: 'assets/argentina.png'
    };

    function tabButton(tab) {
        return document.getElementById('btn-' + tab);
    }

    function decorateButton(button, tab) {
        if (!button || button.querySelector('.nav-icon')) return;
        var label = document.createElement('span');
        label.className = 'nav-label';
        if (tab === 'roadtogs') {
            label.innerHTML = '<span class="desktop-only">Points Breakdown</span><span class="mobile-only">Points</span>';
        } else {
            label.textContent = TAB_LABELS[tab] || button.textContent.trim();
        }
        while (button.firstChild) button.removeChild(button.firstChild);

        var icon = document.createElement('img');
        icon.className = 'nav-icon' + (tab === 'fedbcup' ? ' no-invert' : '');
        icon.src = TAB_ICONS[tab];
        icon.alt = '';
        icon.setAttribute('aria-hidden', 'true');

        button.appendChild(icon);
        button.appendChild(label);
    }

    function section(title, className) {
        var element = document.createElement('div');
        element.className = 'nav-section ' + className;
        var heading = document.createElement('span');
        heading.className = 'nav-section-title';
        heading.textContent = title;
        element.appendChild(heading);
        return element;
    }

    function buildNavigation() {
        var sidebar = document.getElementById('sidebar');
        if (!sidebar || sidebar.dataset.appShellReady === '1') return;
        sidebar.dataset.appShellReady = '1';

        var primary = section('Top pages', 'nav-primary');
        var secondary = section('Explore', 'nav-secondary');
        secondary.id = 'nav-secondary';

        PRIMARY_TABS.forEach(function (tab) {
            var button = tabButton(tab);
            decorateButton(button, tab);
            if (button) primary.appendChild(button);
        });

        var more = document.createElement('button');
        more.type = 'button';
        more.className = 'nav-more-toggle';
        more.id = 'nav-more-toggle';
        more.setAttribute('aria-controls', 'nav-secondary');
        more.setAttribute('aria-expanded', 'false');
        more.innerHTML = '<span class="nav-more-icon" aria-hidden="true">&bull;&bull;&bull;</span><span>More</span>';
        more.addEventListener('click', function () { toggleMobileMore(); });
        primary.appendChild(more);

        SECONDARY_TABS.forEach(function (tab) {
            var button = tabButton(tab);
            decorateButton(button, tab);
            if (button) secondary.appendChild(button);
        });

        sidebar.appendChild(primary);
        sidebar.appendChild(secondary);

        var backdrop = document.createElement('button');
        backdrop.type = 'button';
        backdrop.className = 'nav-sheet-backdrop';
        backdrop.id = 'nav-more-backdrop';
        backdrop.setAttribute('aria-label', 'Close navigation');
        backdrop.addEventListener('click', closeMobileMore);
        document.body.appendChild(backdrop);
    }

    function buildMobileHeader() {
        var main = document.querySelector('.main-content');
        if (!main || main.querySelector('.mobile-app-header')) return;

        var header = document.createElement('header');
        header.className = 'mobile-app-header';
        header.innerHTML =
            '<img src="assets/wtarg-app-icon.png" alt="" class="mobile-app-logo">' +
            '<div class="mobile-app-heading"><strong id="mobile-app-title">Home</strong></div>' +
            '<button type="button" class="mobile-header-theme" aria-label="Toggle dark mode">' +
            '<svg class="dm-icon dm-icon-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z" fill="currentColor"/></svg>' +
            '<svg class="dm-icon dm-icon-sun" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>' +
            '</button>';
        header.querySelector('.mobile-header-theme').addEventListener('click', function () {
            if (typeof window.toggleDarkMode === 'function') window.toggleDarkMode();
        });
        main.insertBefore(header, main.firstChild);
    }

    function reorderHome() {
        var grid = document.querySelector('#view-home .home-grid');
        if (!grid || grid.dataset.priorityReady === '1') return;
        grid.dataset.priorityReady = '1';

        var order = PRIMARY_TABS.concat(SECONDARY_TABS);
        order.forEach(function (tab, index) {
            var button = Array.prototype.find.call(grid.children, function (item) {
                return (item.getAttribute('onclick') || '').indexOf("'" + tab + "'") !== -1;
            });
            if (!button) return;
            button.classList.toggle('home-priority', index < PRIMARY_TABS.length);
            grid.appendChild(button);
        });

    }

    function repositionPointsHeading() {
        var view = document.getElementById('view-roadtogs');
        var controls = view && view.querySelector('.roadtogs-controls');
        var header = view && view.querySelector(':scope > .header-row');
        var points = controls && document.getElementById('roadtogs-points-total');
        if (!controls || !header || !points) return;
        header.classList.add('roadtogs-title-row');
        controls.insertBefore(header, points);
    }

    function currentTab() {
        var active = document.querySelector('.menu-item.active');
        if (active && active.id) return active.id.replace(/^btn-/, '');
        return 'home';
    }

    function updateShell(tab) {
        var normalized = TAB_LABELS[tab] ? tab : currentTab();
        var title = document.getElementById('mobile-app-title');
        if (title) title.textContent = TAB_LABELS[normalized] || 'WTARG';

        var more = document.getElementById('nav-more-toggle');
        if (more) more.classList.toggle('active', SECONDARY_TABS.indexOf(normalized) !== -1);
        document.body.dataset.activeTab = normalized;
    }

    function closeMobileMore() {
        document.body.classList.remove('mobile-more-open');
        var sidebar = document.getElementById('sidebar');
        var more = document.getElementById('nav-more-toggle');
        if (sidebar) sidebar.classList.remove('more-open');
        if (more) more.setAttribute('aria-expanded', 'false');
    }

    function toggleMobileMore(forceOpen) {
        var open = typeof forceOpen === 'boolean'
            ? forceOpen
            : !document.body.classList.contains('mobile-more-open');
        document.body.classList.toggle('mobile-more-open', open);
        var sidebar = document.getElementById('sidebar');
        var more = document.getElementById('nav-more-toggle');
        if (sidebar) sidebar.classList.toggle('more-open', open);
        if (more) more.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    window.closeMobileMore = closeMobileMore;
    window.toggleMobileMore = toggleMobileMore;

    function wrapTabSwitch() {
        if (typeof window.switchTab !== 'function' || window.switchTab.__appShellWrapped) return;
        var original = window.switchTab;
        var wrapped = function (tabName) {
            var tab = String(tabName || 'home').trim().toLowerCase();
            original(tab);
            updateShell(tab);
            closeMobileMore();
        };
        wrapped.__appShellWrapped = true;
        window.switchTab = wrapped;
    }

    function init() {
        buildNavigation();
        buildMobileHeader();
        reorderHome();
        repositionPointsHeading();
        wrapTabSwitch();
        updateShell(currentTab());

        var sidebar = document.getElementById('sidebar');
        if (sidebar && window.MutationObserver) {
            new MutationObserver(function () { updateShell(currentTab()); })
                .observe(sidebar, { subtree: true, attributes: true, attributeFilter: ['class'] });
        }

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') closeMobileMore();
        });
        window.addEventListener('resize', function () {
            if (window.innerWidth > 768) closeMobileMore();
        });

    }

    function initAndReveal() {
        try {
            init();
        } finally {
            // The generated app starts hidden so its default home view cannot
            // flash beside the destination navigation while the document parses.
            // Routing is registered before this initializer, so revealing here
            // paints the selected page and completed shell together.
            document.documentElement.classList.remove('app-booting');
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAndReveal, { once: true });
    } else {
        initAndReveal();
    }
})();
