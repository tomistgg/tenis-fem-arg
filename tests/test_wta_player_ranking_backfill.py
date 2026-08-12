from populate_data.backfill_wta_player_rankings import (
    build_parser,
    fetch_player_ranking_rows,
    merge_ranking_rows,
    parse_player_id,
    parse_weekly_singles_rankings,
)

PAYLOAD = {
    "player": {
        "id": 50020,
        "firstName": "Chris",
        "lastName": "Evert",
        "fullName": "Chris Evert",
        "countryCode": "USA",
        "dateOfBirth": "1954-12-21",
    },
    "weeklyRankings": [
        {
            "singlesRanking": 2,
            "doublesRanking": 12,
            "rankedAt": "1983-01-17T00:00:00Z",
        },
        {
            "singlesRanking": 3,
            "doublesRanking": 10,
            "rankedAt": "1982-12-05T00:00:00Z",
        },
        {
            "singlesRanking": 9999,
            "doublesRanking": 9999,
            "rankedAt": "1983-01-10T00:00:00Z",
        },
    ],
}


def ranking_row(week_date, player_id, rank, player="Player"):
    return {
        "week_date": week_date,
        "id": player_id,
        "rank": str(rank),
        "points": "",
        "player": player,
        "country": "USA",
        "dob": "1960-01-01",
    }


def test_parse_player_id_accepts_id_and_profile_urls():
    assert parse_player_id("50020") == "50020"
    assert parse_player_id(
        "https://www.wtatennis.com/legends/50020/chris-evert/stats"
    ) == "50020"
    assert parse_player_id(
        "https://api.wtatennis.com/tennis/players/190001/ranking?"
        "from=1980-01-01&to=2000-12-31&aggregation-method=weekly"
    ) == "190001"


def test_parser_requires_explicit_opt_in_to_apply_around_conflicts():
    parser = build_parser()

    default_args = parser.parse_args(["--player-id", "190001"])
    opted_in_args = parser.parse_args(
        ["--player-id", "190001", "--keep-existing-conflicts"]
    )

    assert default_args.keep_existing_conflicts is False
    assert opted_in_args.keep_existing_conflicts is True


def test_official_response_cache_makes_batches_resumable(tmp_path, monkeypatch):
    class Response:
        @staticmethod
        def json():
            return PAYLOAD

    requests = []

    def fake_get(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setattr(
        "populate_data.backfill_wta_player_rankings.get_with_retry",
        fake_get,
    )
    first = fetch_player_ranking_rows(
        "50020",
        from_year=1983,
        to_year=1999,
        cache_dir=tmp_path,
    )

    def unexpected_get(*args, **kwargs):
        raise AssertionError("cached player should not be requested again")

    monkeypatch.setattr(
        "populate_data.backfill_wta_player_rankings.get_with_retry",
        unexpected_get,
    )
    second = fetch_player_ranking_rows(
        "50020",
        from_year=1983,
        to_year=1999,
        cache_dir=tmp_path,
    )

    assert first == second
    assert len(requests) == 1
    assert (tmp_path / "50020_1983_1999.json").exists()


def test_parser_selects_weekly_singles_and_year_range():
    profile, rankings = parse_weekly_singles_rankings(
        PAYLOAD, from_year=1983, to_year=1999
    )

    assert profile.player_id == "50020"
    assert profile.name == "Chris Evert"
    assert profile.country == "USA"
    assert profile.dob == "1954-12-21"
    assert rankings == [("1983-01-17", 2)]


def test_merge_adds_absent_player_week_without_changing_existing_rows():
    existing = [ranking_row("1983-01-17", "1", 1, "Existing")]
    incoming = [
        ranking_row("1983-01-17", "1", 1, "Existing"),
        ranking_row("1983-01-17", "2", 2, "New"),
    ]

    result = merge_ranking_rows(existing, incoming)

    assert result.unchanged == 1
    assert result.conflicts == []
    assert result.date_aliases == []
    assert result.skipped_new_weeks == []
    assert result.additions == [ranking_row("1983-01-17", "2", 2, "New")]
    assert [row["id"] for row in result.rows] == ["1", "2"]


def test_merge_reports_existing_rank_conflict_without_overwriting_it():
    existing = [ranking_row("1983-01-17", "1", 1, "Existing")]
    incoming = [ranking_row("1983-01-17", "1", 2, "Existing")]

    result = merge_ranking_rows(existing, incoming)

    assert result.additions == []
    assert result.rows == existing
    assert result.conflicts == [{
        "api_week_date": "1983-01-17",
        "csv_week_date": "1983-01-17",
        "id": "1",
        "player": "Existing",
        "existing_rank": "1",
        "wta_api_rank": "2",
    }]


def test_merge_treats_same_rank_with_nearby_date_as_same_week():
    existing = [ranking_row("1991-03-11", "190001", 4, "Gabriela Sabatini")]
    incoming = [ranking_row("1991-03-12", "190001", 4, "Gabriela Sabatini")]

    result = merge_ranking_rows(existing, incoming)

    assert result.additions == []
    assert result.unchanged == 1
    assert result.date_aliases == [{
        "api_week_date": "1991-03-12",
        "csv_week_date": "1991-03-11",
        "id": "190001",
        "player": "Gabriela Sabatini",
        "rank": "4",
    }]


def test_merge_does_not_create_one_player_week_without_opt_in():
    existing = [ranking_row("1983-01-17", "1", 1, "Existing")]
    incoming = [ranking_row("1983-01-24", "2", 20, "New")]

    result = merge_ranking_rows(existing, incoming)

    assert result.additions == []
    assert result.skipped_new_weeks == [ranking_row("1983-01-24", "2", 20, "New")]

    opted_in = merge_ranking_rows(existing, incoming, allow_new_weeks=True)
    assert opted_in.additions == [ranking_row("1983-01-24", "2", 20, "New")]
