"""Regression tests for live-DB mock isolation and n8n HTTP workflow contract alignment."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.enrichers import spotify_resolver
from src.scrapers import apify_twitter, spotify_miner, youtube_scraper

HTTP_WORKFLOW_PATH = Path("n8n/workflow_unified_beatmatch_http.json")


def test_spotify_resolver_empty_queue_does_not_insert_demo_records_in_live_db():
    """Empty queue on a live database must exit cleanly without injecting synthetic demo artists."""
    mock_conn = MagicMock()
    with patch("src.enrichers.spotify_resolver.is_database_configured", return_value=True), \
         patch("src.enrichers.spotify_resolver.get_connection", return_value=mock_conn), \
         patch("src.enrichers.spotify_resolver.release_connection"), \
         patch("src.enrichers.spotify_resolver.get_pending_queue", return_value=[]), \
         patch("src.reconciler.insert_discovered_lead") as mock_insert:
        spotify_resolver.run_resolver(batch_size=5, allow_mock=False)
        mock_insert.assert_not_called()


def test_scrapers_never_insert_mock_leads_into_configured_live_database():
    """When DATABASE_URL is configured, missing external API keys must never seed mock leads into Postgres."""
    with patch("src.scrapers.youtube_scraper.is_database_configured", return_value=True), \
         patch.dict("os.environ", {"YOUTUBE_API_KEY": ""}, clear=False), \
         patch("src.scrapers.youtube_scraper.insert_discovered_lead") as mock_yt_insert:
        comments = youtube_scraper.scrape_youtube_comments(["https://www.youtube.com/watch?v=test"])
        assert comments == []
        youtube_scraper.filter_and_insert_comments(youtube_scraper.get_mock_comments())
        mock_yt_insert.assert_not_called()

    with patch("src.scrapers.apify_twitter.is_database_configured", return_value=True), \
         patch("src.scrapers.apify_twitter.is_token_missing", True), \
         patch("src.scrapers.apify_twitter.insert_discovered_lead") as mock_tw_insert:
        tweets = apify_twitter.scrape_tweets()
        assert tweets == []
        apify_twitter.parse_and_insert_tweets(apify_twitter.get_mock_tweets())
        mock_tw_insert.assert_not_called()

    with patch("src.scrapers.spotify_miner.is_database_configured", return_value=True), \
         patch("src.scrapers.spotify_miner.insert_discovered_lead") as mock_sp_insert:
        artists = spotify_miner.mine_spotify_artists(sp=None)
        assert artists == []
        spotify_miner.insert_discovered_artists(spotify_miner.get_mock_artists())
        mock_sp_insert.assert_not_called()


def test_n8n_http_workflow_expressions_and_contracts():
    """Validate n8n HTTP workflow expressions, node names, Supabase PKs, and SIPA async polling."""
    payload = json.loads(HTTP_WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = payload["nodes"]
    connections = payload["connections"]
    node_names = {node["name"] for node in nodes}

    # 1. All referenced connection targets must exist in nodes
    for source_name, conn_map in connections.items():
        assert source_name in node_names, f"Dangling source node in connections: {source_name}"
        for branch_list in conn_map.get("main", []):
            for edge in branch_list:
                assert edge["node"] in node_names, f"Dangling target node: {edge['node']}"

    # 2. Every header parameter referencing $env / Mustache must use leading '=' expression prefix
    for node in nodes:
        params = node.get("parameters", {})
        header_params = params.get("headerParameters", {}).get("parameters", [])
        for hdr in header_params:
            val = str(hdr.get("value", ""))
            if "{{" in val or "$env" in val:
                assert val.startswith("="), (
                    f"Node '{node['name']}' header '{hdr.get('name')}' missing leading '=': {val}"
                )

        # 3. Node calling /run/sipa_cleaner must be accurately named (not Gemini Classifier)
        if "/run/sipa_cleaner" in str(params.get("url", "")):
            assert "gemini" not in node["name"].lower()
            assert "sipa" in node["name"].lower()

        # 4. Supabase filters/updates on artists table must reference spotify_id, not missing 'id' column
        if node.get("type") == "n8n-nodes-base.supabase" and params.get("table") == "artists":
            for cond in params.get("filters", {}).get("conditions", []):
                assert cond.get("key") != "id", (
                    f"Node '{node['name']}' queries nonexistent 'id' column instead of 'spotify_id'"
                )
            if "id" in params:
                assert "$json.id" not in str(params["id"]), (
                    f"Node '{node['name']}' references nonexistent $json.id"
                )

    # 5. Async 202 runner polling before export_to_monday
    urls = [str(n.get("parameters", {}).get("url", "")) for n in nodes]
    assert any("/jobs/" in u for u in urls), "Workflow must poll GET /jobs/<job_id> for async 202 jobs"
