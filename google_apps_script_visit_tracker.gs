/**
 * Anonymous aggregate analytics receiver for WTARG.
 *
 * Copy this complete file into the spreadsheet-bound Apps Script project. It
 * creates a NEW "Anonymous Page Views" sheet as needed. Deploy a new web-app
 * version after saving it.
 *
 * The browser sends the country returned by a country-only IP lookup. It does
 * not send the IP, city, region, full URL, referrer, UTM value, cookie or a
 * session identifier.
 */

const ANALYTICS_SCHEMA_VERSION_ = 2;

const PAGE_VIEW_HEADERS_ = [
  'Day (UTC)',
  'Country',
  'Page Type',
  'Content ID',
  'Views',
  '_Aggregate Key'
];

const LEGACY_PAGE_VIEW_HEADERS_ = [
  'Day (UTC)',
  'Page Type',
  'Content ID',
  'Views',
  'Updated At (UTC)',
  '_Aggregate Key'
];

const LEGACY_COUNTRY_PAGE_VIEW_HEADERS_ = [
  'Day (UTC)',
  'Country',
  'Page Type',
  'Content ID',
  'Views',
  'Updated At (UTC)',
  '_Aggregate Key'
];

const PAGE_VIEWS_SHEET_NAME_ = 'Anonymous Page Views';
const ANALYTICS_EVENT_FIELDS_ = new Set([
  'country',
  'page_type',
  'content_id'
]);

const ALLOWED_ANALYTICS_PAGES_ = new Set([
  'home',
  'upcoming',
  'entrylists',
  'draws',
  'calendar',
  'rankings',
  'roadtogs',
  'history',
  'fedbcup',
  'tstrength'
]);

const ANALYTICS_FILTER_PARAMS_ = {
  home: [],
  upcoming: ['q'],
  entrylists: ['t', 'prio'],
  draws: ['t', 'type', 'round'],
  calendar: ['level', 'continent', 'surface'],
  rankings: ['q', 'scope', 'date'],
  roadtogs: ['player'],
  history: [
    'page', 'mcat', 'qualy', 'player', 'surface', 'round', 'result',
    'year', 't', 'category', 'opp', 'oppcountry', 'entry', 'seed',
    'type', 'asrank', 'asmode', 'vsrank', 'vsmode'
  ],
  fedbcup: ['view', 'player'],
  tstrength: ['year', 'level', 'surface', 'region', 'draw', 'sort']
};

const SAFE_CONTENT_PART_RE_ = /^([a-z][a-z0-9]*)=([a-z0-9][a-z0-9,.-]{0,159})$/;
const SAFE_LEGACY_PLAYER_SLUG_RE_ = /^[a-z0-9](?:[a-z0-9-]{0,79})?$/;
const MAX_CONTENT_ID_LENGTH_ = 1024;
const UNSAFE_COUNTRY_RE_ = /[\u0000-\u001f<>|]/;

function analyticsJsonResponse_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Read-only deployment check. Open the web-app URL in a browser after
 * deploying; schema_version must be 2 for filtered Content IDs to be accepted.
 */
function doGet() {
  return analyticsJsonResponse_({
    ok: true,
    schema_version: ANALYTICS_SCHEMA_VERSION_,
    accepts_filter_content_ids: true
  });
}

function parseAnalyticsRequest_(e) {
  const contents = e && e.postData ? String(e.postData.contents || '') : '';
  if (contents === '' || contents.length > 2048) {
    throw new Error('Analytics request body is missing or too large');
  }

  const data = JSON.parse(contents);
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Analytics request body must be a JSON object');
  }
  const unexpectedFields = Object.keys(data).filter(
    (field) => !ANALYTICS_EVENT_FIELDS_.has(field)
  );
  if (unexpectedFields.length) throw new Error('Unexpected analytics fields');
  return data;
}

function doPost(e) {
  try {
    const data = parseAnalyticsRequest_(e);
    const event = normalizeAggregateEvent_(data);
    const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
    if (!spreadsheet) {
      throw new Error('Bind this Apps Script project to the destination spreadsheet');
    }
    const result = recordAggregateEvent_(spreadsheet, event);
    return analyticsJsonResponse_({
      ok: true,
      schema_version: ANALYTICS_SCHEMA_VERSION_,
      views: result.views
    });
  } catch (error) {
    console.error('Anonymous analytics request failed', error);
    return analyticsJsonResponse_({
      ok: false,
      schema_version: ANALYTICS_SCHEMA_VERSION_,
      error: String(error.message || error)
    });
  }
}

function normalizeAnalyticsPageType_(value, allowEmpty) {
  const pageType = value == null ? '' : String(value).trim().toLowerCase();
  if (allowEmpty && pageType === '') return '';
  if (!ALLOWED_ANALYTICS_PAGES_.has(pageType)) {
    throw new Error('Invalid analytics page_type');
  }
  return pageType;
}

function normalizeAnalyticsContentId_(value, pageType) {
  const contentId = value == null ? '' : String(value).trim().toLowerCase();
  if (contentId === '') return '';
  // Keep existing Road-to-GS aggregate keys stable across this rollout.
  if (pageType === 'roadtogs' && SAFE_LEGACY_PLAYER_SLUG_RE_.test(contentId)) {
    return contentId;
  }

  if (contentId.length > MAX_CONTENT_ID_LENGTH_) {
    throw new Error('Analytics content_id is too long');
  }
  const allowedKeys = ANALYTICS_FILTER_PARAMS_[pageType] || [];
  const parts = contentId.split('&');
  let previousKeyIndex = -1;
  const normalizedParts = parts.map((part) => {
    const match = part.match(SAFE_CONTENT_PART_RE_);
    if (!match) throw new Error('Invalid analytics content_id');
    const keyIndex = allowedKeys.indexOf(match[1]);
    if (keyIndex === -1 || keyIndex <= previousKeyIndex) {
      throw new Error('Unexpected or unordered analytics filter');
    }
    previousKeyIndex = keyIndex;
    return match[1] + '=' + match[2];
  });
  return normalizedParts.join('&');
}

function normalizeAnalyticsCountry_(value) {
  const country = value == null ? '' : String(value).trim();
  if (country === '') return 'Unknown';
  if (
    country.length > 64
    || /^[=+\-@]/.test(country)
    || UNSAFE_COUNTRY_RE_.test(country)
  ) {
    throw new Error('Invalid analytics country');
  }
  return country;
}

function normalizeAggregateEvent_(data) {
  const pageType = normalizeAnalyticsPageType_(data.page_type, false);
  return {
    day: Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd'),
    country: normalizeAnalyticsCountry_(data.country),
    pageType: pageType,
    contentId: normalizeAnalyticsContentId_(data.content_id, pageType)
  };
}

function migrateLegacyPageViews_(sheet) {
  if (!sheet || sheet.getLastRow() === 0) return;
  let actualHeaders = sheet
    .getRange(1, 1, 1, LEGACY_PAGE_VIEW_HEADERS_.length)
    .getDisplayValues()[0];
  const lastRow = sheet.getLastRow();
  if (actualHeaders.join('\u001f') === LEGACY_PAGE_VIEW_HEADERS_.join('\u001f')) {
    sheet.insertColumnAfter(1);
    sheet.getRange(1, 2).setValue('Country');
    if (lastRow > 1) sheet.getRange(2, 2, lastRow - 1, 1).setValue('Unknown');
  }

  actualHeaders = sheet
    .getRange(1, 1, 1, LEGACY_COUNTRY_PAGE_VIEW_HEADERS_.length)
    .getDisplayValues()[0];
  if (
    actualHeaders.join('\u001f')
    !== LEGACY_COUNTRY_PAGE_VIEW_HEADERS_.join('\u001f')
  ) return;

  sheet.deleteColumn(6);
  if (lastRow > 1) {
    const dimensions = sheet.getRange(2, 1, lastRow - 1, 4).getDisplayValues();
    const keys = dimensions.map((row) => [row.join('|')]);
    sheet.getRange(2, PAGE_VIEW_HEADERS_.length, keys.length, 1).setValues(keys);
  }
  sheet.getRange(1, 1, sheet.getMaxRows(), 4).setNumberFormat('@');
  sheet.hideColumns(PAGE_VIEW_HEADERS_.length);
}

function ensureAggregateSheet_(sheet, headers, textColumnCount) {
  if (!sheet) throw new Error('Analytics destination sheet is required');
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.getRange(1, 1, sheet.getMaxRows(), textColumnCount).setNumberFormat('@');
    sheet.hideColumns(headers.length);
    sheet.setFrozenRows(1);
    return;
  }

  const actualHeaders = sheet.getRange(1, 1, 1, headers.length).getDisplayValues()[0];
  if (actualHeaders.join('\u001f') !== headers.join('\u001f')) {
    throw new Error(
      'Analytics sheet has the wrong schema. Do not mix raw visits with aggregates.'
    );
  }
}

function incrementAggregate_(sheet, headers, keyParts) {
  ensureAggregateSheet_(sheet, headers, keyParts.length);
  const lastRow = sheet.getLastRow();
  const key = keyParts.join('|');
  const viewsColumn = keyParts.length + 1;
  const keyColumn = headers.length;
  const match = lastRow > 1
    ? sheet
      .getRange(2, keyColumn, lastRow - 1, 1)
      .createTextFinder(key)
      .matchEntireCell(true)
      .findNext()
    : null;

  if (match) {
    const rowNumber = match.getRow();
    const views = (Number(sheet.getRange(rowNumber, viewsColumn).getValue()) || 0) + 1;
    sheet.getRange(rowNumber, viewsColumn).setValue(views);
    return views;
  }

  const nextRow = lastRow + 1;
  if (nextRow > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), 100);
  sheet.getRange(nextRow, 1, 1, headers.length).setValues([[
    ...keyParts,
    1,
    key
  ]]);
  return 1;
}

function recordAggregateEvent_(spreadsheet, event) {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);

  try {
    const pageViewsSheet = spreadsheet.getSheetByName(PAGE_VIEWS_SHEET_NAME_)
      || spreadsheet.insertSheet(PAGE_VIEWS_SHEET_NAME_);
    migrateLegacyPageViews_(pageViewsSheet);
    const views = incrementAggregate_(pageViewsSheet, PAGE_VIEW_HEADERS_, [
      event.day,
      event.country,
      event.pageType,
      event.contentId
    ]);

    return { views: views };
  } finally {
    lock.releaseLock();
  }
}
