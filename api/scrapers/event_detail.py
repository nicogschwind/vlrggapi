"""
Scraper for individual VLR.GG event detail pages.

Extracts event header info, prize pool breakdown, participating teams,
and group/stage standings tables. Consolidates match results when available.
"""
import asyncio
import logging

from selectolax.parser import HTMLParser

from utils.cache_manager import cache_manager
from utils.constants import VLR_BASE_URL, CACHE_TTL_EVENTS
from utils.error_handling import handle_scraper_errors, raise_for_upstream_status
from utils.html_parsers import (
    extract_text_content,
    normalize_image_url,
    parse_href_id_slug,
    build_full_url,
    parse_match_timestamp,
)
from utils.http_client import fetch_with_retries, get_http_client
from utils.id_mapper import id_mapper

logger = logging.getLogger(__name__)


def _parse_event_header(html: HTMLParser) -> dict:
    """Extract event name, series, dates, prize pool, location, and logo."""
    series = ""
    name = ""
    subtitle = ""
    dates = ""
    prize = ""
    location = ""
    logo = ""

    header = html.css_first(".event-header") or html.css_first(".wf-card")
    if not header:
        return {"name": name, "series": series, "subtitle": subtitle,
                "dates": dates, "prize": prize, "location": location, "logo": logo}

    logo_elem = header.css_first(".event-header-thumb img") or header.css_first("img")
    if logo_elem:
        logo = normalize_image_url(logo_elem.attributes.get("src", ""))

    # Series link (first anchor inside .event-desc-inner before the h1)
    desc_inner = header.css_first(".event-desc-inner")
    if desc_inner:
        series_link = desc_inner.css_first("a")
        if series_link:
            series = extract_text_content(series_link)

    # Event name
    title = header.css_first("h1.wf-title")
    if title:
        name = extract_text_content(title)

    # Subtitle
    sub = header.css_first(".event-desc-subtitle")
    if sub:
        subtitle = extract_text_content(sub)

    # Key-value items (dates, prize, location)
    for item in header.css(".event-desc-item"):
        label_elem = item.css_first(".event-desc-item-label")
        value_elem = item.css_first(".event-desc-item-value")
        if not label_elem or not value_elem:
            continue
        label = extract_text_content(label_elem).rstrip(":")
        value = extract_text_content(value_elem)
        if label.lower() == "dates":
            dates = value
        elif label.lower() == "prize":
            prize = value
        elif label.lower() in ("location", "venue"):
            location = value

    return {
        "name": name,
        "series": series,
        "subtitle": subtitle,
        "dates": dates,
        "prize": prize,
        "location": location,
        "logo": logo,
    }


def _parse_prizes(html: HTMLParser) -> list[dict]:
    """Parse the prize breakdown table from the event page."""
    prizes: list[dict] = []

    # Find the prize card specifically (look for the "Prize Distribution" label)
    prize_card = None
    for label in html.css(".wf-label"):
        if "Prize Distribution" in extract_text_content(label):
            prize_card = label.next
            while prize_card and "wf-card" not in prize_card.attributes.get("class", ""):
                prize_card = prize_card.next
            break

    if not prize_card:
        return prizes

    # The prize table uses div.wf-ptable with div.row elements
    ptable = prize_card.css_first(".wf-ptable")
    if not ptable:
        return prizes

    rows = ptable.css(".row")
    for row in rows[1:]:  # skip header row
        cells = row.css(".cell")
        if len(cells) < 3:
            continue

        placement = extract_text_content(cells[0])
        amount = extract_text_content(cells[1])

        # Team cell: contains an <a> link with name and optional region
        team_name = ""
        team_id = ""
        team_logo = ""
        team_region = ""

        team_link = cells[2].css_first("a")
        if team_link:
            href = team_link.attributes.get("href", "")
            team_id, _ = parse_href_id_slug(href)
            name_div = team_link.css_first(".event-group-team-name") or team_link.css_first(".text-of")
            if name_div:
                region_div = name_div.css_first(".ge-text-light")
                if region_div:
                    team_name = extract_text_content(name_div).replace(extract_text_content(region_div), "").strip()
                    team_region = extract_text_content(region_div)
                else:
                    team_name = extract_text_content(name_div)
            else:
                team_name = extract_text_content(team_link)
            img = team_link.css_first("img")
            if img:
                team_logo = normalize_image_url(img.attributes.get("src", ""))

        prizes.append({
            "placement": placement,
            "amount": amount,
            "team": {
                "id": team_id,
                "name": team_name,
                "logo": team_logo,
                "region": team_region,
            },
        })
        id_mapper.register_team(team_name, team_id)

    return prizes


def _parse_event_teams(html: HTMLParser) -> list[dict]:
    """Parse participating teams from event-team cards."""
    teams: list[dict] = []

    for card in html.css(".wf-card.event-team"):
        # Team name link
        name_link = card.css_first(".event-team-name")
        team_name = ""
        team_id = ""
        if name_link:
            team_name = extract_text_content(name_link)
            href = name_link.attributes.get("href", "")
            team_id, _ = parse_href_id_slug(href)

        # Team logo
        logo_img = card.css_first("img")
        team_logo = ""
        if logo_img:
            team_logo = normalize_image_url(logo_img.attributes.get("src", ""))

        # Players
        players: list[dict] = []
        for player_link in card.css(".event-team-players-item"):
            href = player_link.attributes.get("href", "")
            p_id, _ = parse_href_id_slug(href)
            p_name = extract_text_content(player_link)
            # Parse flag class (e.g. "flag mod-us")
            flag = ""
            flag_elem = player_link.css_first(".flag")
            if flag_elem:
                flag_class = flag_elem.attributes.get("class", "")
                flag = flag_class.replace("flag ", "").replace(" mod-", "_")
            players.append({"id": p_id, "name": p_name, "flag": flag})

        # Qualification note
        note = ""
        note_elem = card.css_first(".event-team-note")
        if note_elem:
            note_link = note_elem.css_first("a")
            if note_link:
                note = extract_text_content(note_link)

        teams.append({
            "id": team_id,
            "name": team_name,
            "logo": team_logo,
            "players": players,
            "qualification": note,
        })
        id_mapper.register_team(team_name, team_id)
        for p in players:
            id_mapper.register_team(p["name"], "")

    return teams


def _parse_standings(html: HTMLParser) -> list[dict]:
    """Parse group/stage standings tables from the event page.

    Handles both standard wf-ptable layouts and standard <table> blocks
    used for tournament groups.
    """
    standings: list[dict] = []

    # Strategy 1: wf-table mod-group (Standard tournament group tables)
    for table in html.css("table.mod-group"):
        group_name = ""
        header = table.css_first("th.mod-title")
        if header:
            group_name = extract_text_content(header)

        # Get column headers from thead
        headers = []
        thead = table.css_first("thead")
        if thead:
            # We want headers after the title column (which usually has colspan=2)
            title_cell = thead.css_first("th.mod-title")
            offset = int(title_cell.attributes.get("colspan", 1)) if title_cell else 1
            
            # The row often has multiple th/td elements
            header_cells = thead.css("th")
            for cell in header_cells:
                text = extract_text_content(cell)
                if text and text != group_name:
                    headers.append(text)

        rows = []
        for tr in table.css("tbody tr"):
            cells = tr.css("td")
            if not cells:
                continue

            # Team column
            team_data = {"name": "", "id": "", "logo": "", "country": ""}
            team_link = tr.css_first("a.event-group-team")
            if team_link:
                href = team_link.attributes.get("href", "")
                team_data["id"], _ = parse_href_id_slug(href)
                name_container = team_link.css_first(".event-group-team-name")
                if name_container:
                    country_elem = name_container.css_first(".ge-text-light")
                    if country_elem:
                        team_data["country"] = extract_text_content(country_elem)
                        team_data["name"] = extract_text_content(name_container).replace(team_data["country"], "").strip()
                    else:
                        team_data["name"] = extract_text_content(name_container)
            
            logo_img = tr.css_first("img.event-group-team-logo")
            if logo_img:
                team_data["logo"] = normalize_image_url(logo_img.attributes.get("src", ""))

            # Stats columns
            # VLR usually has team name span multiple columns. 
            # We look for cells with 'mod-stat' or just numeric content.
            row_stats = {"team": team_data}
            stat_cells = tr.css("td.mod-stat")
            for idx, cell in enumerate(stat_cells):
                label = headers[idx] if idx < len(headers) else f"stat_{idx}"
                row_stats[label] = extract_text_content(cell)
            
            rows.append(row_stats)

        standings.append({"stage": group_name, "type": "table", "rows": rows})

    # Strategy 2: wf-ptable (Fallback for newer/different layouts)
    for card in html.css(".wf-card"):
        ptable = card.css_first(".wf-ptable")
        if not ptable:
            continue
        
        # Skip the prize table if it was already handled
        parent_classes = card.attributes.get("class", "")
        if "mod-dark" in parent_classes:
            # We check the label before it to see if it's the prize distribution
            is_prize_table = False
            prev_node = card.prev
            while prev_node:
                # Selectolax nodes can be text nodes (None tag) or element nodes
                if prev_node.tag:
                    if "wf-label" in prev_node.attributes.get("class", ""):
                        if "Prize Distribution" in extract_text_content(prev_node):
                            is_prize_table = True
                        break
                    # If we hit another card or a different section, stop looking
                    if "wf-card" in prev_node.attributes.get("class", ""):
                        break
                prev_node = prev_node.prev
            
            if is_prize_table:
                continue

        stage = ""
        label_elem = card.css_first(".wf-label") or card.css_first("h2")
        if label_elem:
            stage = extract_text_content(label_elem)

        header_row = ptable.css_first(".row")
        if not header_row:
            continue
        
        headers = [extract_text_content(cell) for cell in header_row.css(".cell")]
        if not headers: continue

        rows = []
        for row in ptable.css(".row")[1:]:
            cells = row.css(".cell")
            if not cells: continue
            row_data = {}
            for idx, cell in enumerate(cells):
                label = headers[idx] if idx < len(headers) else str(idx)
                team_link = cell.css_first("a")
                if team_link and idx == 0:
                    row_data[label] = extract_text_content(team_link)
                else:
                    row_data[label] = extract_text_content(cell)
            rows.append(row_data)

        standings.append({"stage": stage, "type": "ptable", "rows": rows})

    return standings


def _parse_event_matches(html: HTMLParser) -> list:
    """Parse the match list from an event matches page."""
    matches = []
    current_date = ""

    for elem in html.css(".wf-label.mod-large, a.wf-module-item.match-item"):
        classes = elem.attributes.get("class", "")

        if "wf-label" in classes:
            current_date = elem.text(strip=True)
            continue

        href = elem.attributes.get("href", "")
        match_id, _ = parse_href_id_slug(href)
        match_url = build_full_url(href)

        team_elems = elem.css(".match-item-vs-team")
        teams = []
        for te in team_elems:
            name_el = te.css_first(".match-item-vs-team-name")
            score_el = te.css_first(".match-item-vs-team-score")
            name = name_el.text(strip=True) if name_el else "TBD"
            score = score_el.text(strip=True) if score_el else ""
            is_winner = "mod-winner" in te.attributes.get("class", "")
            teams.append({"name": name, "score": score, "is_winner": is_winner})

        while len(teams) < 2:
            teams.append({"name": "TBD", "score": "", "is_winner": False})

        series_el = elem.css_first(".match-item-event-series")
        event_series = series_el.text(strip=True) if series_el else ""

        status_el = elem.css_first(".ml-status")
        eta_el = elem.css_first(".ml-eta")
        match_status = ""
        if status_el:
            match_status = status_el.text(strip=True)
        elif eta_el:
            match_status = eta_el.text(strip=True)

        timestamp = parse_match_timestamp(elem, current_date)

        vods = []
        for vod_el in elem.css(".match-item-vod .wf-tag"):
            vod_text = vod_el.text(strip=True)
            vod_link_el = vod_el if vod_el.tag == "a" else vod_el.parent
            vod_href = vod_link_el.attributes.get("href", "") if vod_link_el else ""
            if vod_href:
                vod_href = build_full_url(vod_href)
            vods.append({"label": vod_text, "url": vod_href})

        note_el = elem.css_first(".match-item-note")
        note = note_el.text(strip=True) if note_el else ""

        matches.append({
            "match_id": match_id,
            "url": match_url,
            "date": current_date,
            "timestamp": timestamp,
            "status": match_status,
            "note": note,
            "event_series": event_series,
            "team1": teams[0],
            "team2": teams[1],
            "vods": vods,
        })
    return matches


@handle_scraper_errors
async def vlr_event_detail(event_id: str, theme: str | None = None) -> dict:
    """Fetch full event detail: header, prizes, teams, standings, and matches.

    Args:
        event_id: Numeric VLR.GG event ID.
        theme: Optional theme preference.
    """
    async def build():
        base_url = f"{VLR_BASE_URL}/event/{event_id}"
        matches_url = f"{VLR_BASE_URL}/event/matches/{event_id}"
        client = get_http_client()

        # Fetch both the main event page and the matches page concurrently
        responses = await asyncio.gather(
            fetch_with_retries(base_url, client=client, theme=theme),
            fetch_with_retries(matches_url, client=client, theme=theme),
        )
        base_resp, matches_resp = responses
        status = base_resp.status_code
        raise_for_upstream_status(status, f"event detail {event_id}")

        base_html = HTMLParser(base_resp.text)
        matches_html = HTMLParser(matches_resp.text)

        header = _parse_event_header(base_html)
        prizes = _parse_prizes(base_html)
        teams = _parse_event_teams(base_html)
        standings = _parse_standings(base_html)
        matches = _parse_event_matches(matches_html)

        data = {
            "data": {
                "status": status,
                "segments": {
                    "event": header,
                    "prizes": prizes,
                    "teams": teams,
                    "standings": standings,
                    "matches": matches,
                },
            }
        }
        return data

    return await cache_manager.get_or_create_async(
        CACHE_TTL_EVENTS, build, "event_detail", event_id, theme
    )
