import pandas as pd

# Load the source files
matches_df = pd.read_csv('fed_bjk_cup_matches.csv')
series_df = pd.read_csv('fed_bjk_cup_series.csv')

# Merge on tieId to get tournament and round details
merged_df = pd.merge(matches_df, series_df, on='tieId', how='left')

# Filter for Argentina matches
arg_matches = merged_df[(merged_df['winnerCountry'] == 'ARG') | (merged_df['loserCountry'] == 'ARG')].copy()

# --- FILTER: Keep only singles matches (names do NOT contain '/') ---
arg_matches = arg_matches[
    (~arg_matches['winnerName'].str.contains('/', na=False)) & 
    (~arg_matches['loserName'].str.contains('/', na=False))
]

# Date formatting: yyyy-MM-dd
arg_matches['match_date_fmt'] = pd.to_datetime(arg_matches['dateMatch']).dt.strftime('%Y-%m-%d')

# Map columns to the target schema
final_df = pd.DataFrame({
    'match_type': 'Fed/BJK Cup',
    'match_date': arg_matches['match_date_fmt'],
    'tournament_name': arg_matches['eventName'],
    'surface': arg_matches['surface'],
    'tournament_country': arg_matches['tournamentCountry'],
    'round_name': arg_matches['roundName'],
    'draw': arg_matches['drawName'],
    'result': arg_matches['result'],
    'result_status': '',
    'winner_entry': '',
    'winner_seed': '',
    'winner_name': arg_matches['winnerName'],
    'winner_country': arg_matches['winnerCountry'],
    'loser_entry': '',
    'loser_seed': '',
    'loser_name': arg_matches['loserName'],
    'loser_country': arg_matches['loserCountry']
})

# Sort by date descending
final_df = final_df.sort_values(by='match_date', ascending=False)

# Save to CSV using comma as delimiter
final_df.to_csv('fed_bjk_matches_arg.csv', index=False, sep=',', encoding='utf-8-sig')

print(f"File updated. Total singles matches found: {len(final_df)}")