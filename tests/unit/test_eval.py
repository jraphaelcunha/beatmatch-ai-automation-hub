"""
Unit tests for the LLM evaluation framework.
Verifies parsing of ground truth, metric computations, and mock evaluation mode.
"""


from eval.evaluate import (
    estimate_tokens,
    mock_classify,
    parse_ground_truth_bool,
    run_evaluation,
)


def test_parse_ground_truth_bool():
    """Verify boolean parser parses true/false string variants correctly."""
    assert parse_ground_truth_bool("TRUE") is True
    assert parse_ground_truth_bool("true") is True
    assert parse_ground_truth_bool("1") is True
    assert parse_ground_truth_bool("yes") is True
    assert parse_ground_truth_bool("FALSE") is False
    assert parse_ground_truth_bool("0") is False
    assert parse_ground_truth_bool("no") is False
    assert parse_ground_truth_bool("invalid") is None


def test_estimate_tokens():
    """Character-to-token heuristic estimation."""
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("") == 1


def test_mock_classify():
    """Mock classifier detects artist keywords vs beatmaker keywords."""
    res_artist = mock_classify("Check out my new single on Spotify! Link in bio - Marcus Kane")
    assert res_artist.is_artist_promotion is True
    assert res_artist.artist_name == "Marcus Kane"

    res_producer = mock_classify("New drum kit and loops for sale. Prod by 808")
    assert res_producer.is_artist_promotion is False


def test_run_evaluation_mock_mode(tmp_path):
    """Run evaluation over a test CSV using mock mode."""
    test_csv = tmp_path / "test_dataset.csv"
    test_csv.write_text(
        "id,text,source,ground_truth_is_artist,ground_truth_artist_name,notes\n"
        "1,Check out my new single 'Sunset',youtube_comment,TRUE,Artist One,Test\n"
        "2,Buy my drum kit,twitter_search,FALSE,,Test\n",
        encoding="utf-8"
    )

    metrics = run_evaluation(str(test_csv), use_mock=True)
    assert metrics.total_evaluated == 2
    assert metrics.true_positives == 1
    assert metrics.true_negatives == 1
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1_score == 1.0
    assert metrics.failures == 0
