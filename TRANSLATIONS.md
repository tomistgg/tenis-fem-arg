# Editing the app's Spanish translations

Open `assets/translations.js`. Change the Spanish text on the **right** of an entry:

```js
"Schedule": "Agenda",
"Entry Lists": "Listas de entrada",
```

Keep the English key on the left, quotation marks, and commas. You can add new
English phrases with the same format. Text is plain text, not HTML. For messages
such as `"Points: {value}"`, keep `{value}` in the Spanish translation: the app
inserts the changing number or other value there. Preserve all placeholders
(including `{start}`, `{end}` and `{total}` in pagination). English templates
must match the UI exactly.

Run `python build_deploy_site.py --output .site` with the project's Python 3.11
environment after editing. The deploy builder copies this file into the site.
Refresh the browser to load the new translations.

If `.site` is a junction to another drive, build into a normal folder inside
the repository (for example `--output .run_staging/language-site`), then copy
that folder's contents into `.site`. The builder does not replace external
junction targets.

English is the default. The ES/EN buttons save the choice in the browser's
`wtarg-language` local-storage entry, shared by the home page and app routes.
If storage is unavailable, switching still works for the current page.

`assets/language.js` translates known interface phrases as tables and controls
render. It keeps original English text for switching back and leaves data,
option values, URLs, player names and tournament names intact. Add
`translate="no"` to any new container that holds proper names. UI text outside
the dictionary falls back to English. Never put player or tournament names in
the dictionary.
