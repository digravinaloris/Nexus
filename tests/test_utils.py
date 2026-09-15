"""
Tests des fonctions utilitaires pures (utils.py). Ne nécessite ni token
Discord ni connexion MongoDB -- s'exécute avec : pytest tests/
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import (
    parse_duration,
    check_caps,
    check_banned_words,
    check_banned_domains,
    format_uptime,
)


class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("30m") == 30 * 60

    def test_hours(self):
        assert parse_duration("2h") == 2 * 3600

    def test_days(self):
        assert parse_duration("1d") == 86400

    def test_weeks(self):
        assert parse_duration("1w") == 604800

    def test_seconds(self):
        assert parse_duration("45s") == 45

    def test_uppercase_and_whitespace(self):
        assert parse_duration("  2H  ") == 2 * 3600

    def test_invalid_unit(self):
        assert parse_duration("5x") == -1

    def test_invalid_format(self):
        assert parse_duration("soon") == -1

    def test_empty_string(self):
        assert parse_duration("") == -1

    def test_negative_not_matched(self):
        assert parse_duration("-5m") == -1


class TestCheckCaps:
    def test_all_caps_flagged(self):
        assert check_caps("THIS MESSAGE IS SHOUTING AT EVERYONE") is True

    def test_normal_text_not_flagged(self):
        assert check_caps("this is a normal message, nothing wrong here") is False

    def test_too_short_to_flag(self):
        # En dessous de CAPS_MIN_LENGTH (10), jamais flag même si 100% majuscules
        assert check_caps("HI") is False

    def test_mixed_case_below_threshold(self):
        assert check_caps("Hello there, how are you doing today?") is False

    def test_custom_threshold_via_cfg(self):
        content = "Some Caps Here"  # ratio de majuscules modéré
        assert check_caps(content, cfg={"automod_caps_ratio": 0.01}) is True
        assert check_caps(content, cfg={"automod_caps_ratio": 0.99}) is False


class TestCheckBannedWords:
    def test_exact_word_match(self):
        assert check_banned_words("you are a badword honestly", ["badword"]) is True

    def test_case_insensitive(self):
        assert check_banned_words("you are a BADWORD honestly", ["badword"]) is True

    def test_whole_word_only_no_substring_match(self):
        # "class" contient "ass" mais ne doit pas déclencher un faux positif
        assert check_banned_words("I love my class today", ["ass"]) is False

    def test_no_match(self):
        assert check_banned_words("this is a totally clean message", ["badword"]) is False

    def test_empty_banned_list(self):
        assert check_banned_words("anything goes here", []) is False


class TestCheckBannedDomains:
    def test_exact_domain_match(self):
        assert check_banned_domains("check http://evil.com/page", ["evil.com"]) is True

    def test_subdomain_also_blocked(self):
        assert check_banned_domains("visit http://sub.evil.com/x", ["evil.com"]) is True

    def test_unrelated_domain_not_blocked(self):
        assert check_banned_domains("visit http://notevil.com/x", ["evil.com"]) is False

    def test_no_links_in_message(self):
        assert check_banned_domains("just a normal message, no links", ["evil.com"]) is False

    def test_https_and_http_both_detected(self):
        assert check_banned_domains("https://evil.com", ["evil.com"]) is True

    def test_empty_banned_list(self):
        assert check_banned_domains("http://anything.com", []) is False


class TestFormatUptime:
    def test_minutes_only(self):
        assert format_uptime(300) == "5m"

    def test_hours_and_minutes_shown_as_hours(self):
        # Sans jours, on affiche heures + minutes (voir implémentation)
        result = format_uptime(3 * 3600 + 5 * 60)
        assert "3h" in result

    def test_days_and_hours(self):
        result = format_uptime(2 * 86400 + 4 * 3600)
        assert "2d" in result and "4h" in result

    def test_zero_seconds(self):
        assert format_uptime(0) == "0m"
