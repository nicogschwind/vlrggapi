import pytest
from selectolax.parser import HTMLParser
from api.scrapers.teams import _parse_team_match_item
from api.scrapers.players import _parse_player_match_item

def test_team_match_date_parsing():
    html_content = """
    <a href="/match/12345/team1-vs-team2" class="m-item">
        <div class="m-item-result mod-win">13 - 5</div>
        <div class="m-item-team">
            <div class="m-item-team-name">Team A</div>
            <div class="m-item-team-tag">TAGA</div>
            <div class="m-item-logo"><img src="/logo1.png"></div>
        </div>
        <div class="m-item-team">
            <div class="m-item-team-name">Team B</div>
            <div class="m-item-team-tag">TAGB</div>
            <div class="m-item-logo"><img src="/logo2.png"></div>
        </div>
        <div class="m-item-event">VCT Champions</div>
        <div class="m-item-date">
            2026/01/24
            <div class="m-item-date-time">7:00 pm</div>
        </div>
    </a>
    """
    html = HTMLParser(html_content)
    item = html.css_first("a.m-item")
    parsed = _parse_team_match_item(item)
    
    assert parsed["date"] == "2026/01/24 7:00 pm"

def test_player_match_date_parsing():
    html_content = """
    <a href="/match/12345/team1-vs-team2" class="wf-card m-item">
        <div class="m-item-result mod-win">2 - 0</div>
        <div class="m-item-team">
            <div class="m-item-team-name">Team A</div>
            <div class="m-item-team-tag">TAGA</div>
            <div class="m-item-logo"><img src="/logo1.png"></div>
        </div>
        <div class="m-item-team">
            <div class="m-item-team-name">Team B</div>
            <div class="m-item-team-tag">TAGB</div>
            <div class="m-item-logo"><img src="/logo2.png"></div>
        </div>
        <div class="m-item-event">VCT Champions</div>
        <div class="m-item-date">
            2026/01/24
            <div class="m-item-date-time">7:00 pm</div>
        </div>
    </a>
    """
    html = HTMLParser(html_content)
    item = html.css_first("a.m-item")
    parsed = _parse_player_match_item(item)
    
    assert parsed["date"] == "2026/01/24 7:00 pm"
